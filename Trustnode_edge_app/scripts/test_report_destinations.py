# -*- coding: utf-8 -*-
"""Where a report is written, and the promise that it never lands on another.

2026-10-23, requested: reports that file themselves into a customer's own
folder tree - a prefix, a tag value, a suffix that depends on what another
tag holds, and a number when the same work is reported more than once.

Two things drove this file.

The first is a data-loss bug that was already there: there was NO collision
handling anywhere. Two reports whose pattern resolved to the same name -
the second test of the same job, a re-run of the same day - silently wrote
over each other and the earlier PDF was gone. A report is a record.

The second is that the destination now depends on tag VALUES, so the
mapping from "what the plant said" to "where the file went" has to be
exact. A rule that matches when it should not sends a customer's report
into the wrong folder, which is worse than not filing it at all.

Nothing in here encodes any particular customer's naming. The rules, the
folders and the names are entirely the operator's; these tests use
deliberately neutral values.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
# Point every data path at a scratch dir BEFORE the import: a test must
# never touch the live workspace.
_TMP = tempfile.mkdtemp(prefix="tn-dest-")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"

from app.services.report_renderer import (          # noqa: E402
    build_report_file_name, resolve_destination, unique_output_path,
)

FAILS = []


def check(name, ok, detail=""):
    print("  {0:62s}: {1}{2}".format(
        name, "PASS" if ok else "FAIL", (" - " + str(detail)[:150]) if not ok else ""))
    if not ok:
        FAILS.append(name)


# A stand-in historian: the condition and the {token} must read THIS.
VALUES = {("", "Stage"): "2", ("", "Result"): "1", ("", "Job"): "A17"}


def fetch(cond, _from, _to):
    key = (str(cond.get("gateway_id") or ""), str(cond.get("tag_name") or ""))
    if key not in VALUES:
        return []
    return [{"value": None, "value_text": VALUES[key]}]


print("Report destinations: rules, tokens, and never overwriting")
print()

# ------------------------------------------------- never overwrite
print("[a report never lands on one that already exists]")
d = Path(_TMP) / "out"
d.mkdir(parents=True, exist_ok=True)

p1 = unique_output_path(d / "report.pdf")
check("a free name is used as-is", p1.name == "report.pdf", p1.name)
p1.write_bytes(b"first")

p2 = unique_output_path(d / "report.pdf")
check("  a second run does NOT overwrite it", p2.name != "report.pdf", p2.name)
p2.write_bytes(b"second")
check("  the first file is still there and unchanged",
      (d / "report.pdf").read_bytes() == b"first",
      "losing a generated report is losing a record")

p3 = unique_output_path(d / "report.pdf")
check("  and a third gets its own name again",
      p3.name not in {p1.name, p2.name}, p3.name)

# ------------------------------------------------- the {n} token
print()
print("[{n} says WHERE the number goes]")
name = build_report_file_name("job_{n}", template_name="T", report_id="r1")
check("{n} survives sanitising", "TNSEQSLOT" in name,
      "the number is only knowable once the folder can be read: " + name)
q1 = unique_output_path(d / name)
check("  it starts at 1", q1.name == "job_1.pdf", q1.name)
q1.write_bytes(b"x")
q2 = unique_output_path(d / name)
check("  then counts up", q2.name == "job_2.pdf", q2.name)
check("  a tag is NOT mistaken for the token",
      "{n}" not in build_report_file_name("{Job}", fetch=fetch),
      "a tag called n must still resolve as a tag")

# ------------------------------------------------- rules
print()
print("[the first matching rule decides the destination]")
RULES = [
    {"enabled": True, "logic": "all",
     "conditions": [{"gateway_id": "", "tag_name": "Stage",
                     "operator": "==", "value": "1"}],
     "output_path": "C:/base/one", "file_name_pattern": "first"},
    {"enabled": True, "logic": "all",
     "conditions": [{"gateway_id": "", "tag_name": "Stage",
                     "operator": "==", "value": "2"}],
     "output_path": "C:/base/two", "file_name_pattern": "second"},
]
got = resolve_destination(RULES, "C:/fallback", "plain", fetch=fetch)
check("the matching rule wins", got == "C:/base/two/second.pdf", got)

got = resolve_destination(
    [{"enabled": True, "conditions": [
        {"gateway_id": "", "tag_name": "Stage", "operator": "==", "value": "9"}],
      "output_path": "C:/never", "file_name_pattern": "no"}],
    "C:/fallback", "plain", fetch=fetch)
check("no rule matching falls back to the boxes above",
      got == "C:/fallback/plain.pdf", got)

got = resolve_destination([], "C:/fallback", "plain", fetch=fetch)
check("  and no rules at all behaves exactly as before",
      got == "C:/fallback/plain.pdf", got)

got = resolve_destination(
    [{"enabled": True, "conditions": [
        {"gateway_id": "", "tag_name": "Result", "operator": "==", "value": "1"}],
      "file_name_pattern": "named"}],
    "C:/fallback", "plain", fetch=fetch)
check("a rule may change only the NAME, keeping the folder",
      got == "C:/fallback/named.pdf", got)

got = resolve_destination(
    [{"enabled": False, "conditions": [
        {"gateway_id": "", "tag_name": "Stage", "operator": "==", "value": "2"}],
      "output_path": "C:/off"}],
    "C:/fallback", "plain", fetch=fetch)
check("a disabled rule is ignored", got == "C:/fallback/plain.pdf", got)

got = resolve_destination(
    [{"enabled": True, "conditions": [], "output_path": "C:/empty"}],
    "C:/fallback", "plain", fetch=fetch)
check("a rule with NO condition matches nothing", got == "C:/fallback/plain.pdf",
      "%s - an empty rule must not capture every report" % got)

print()
print("[ALL vs ANY]")
both = [{"gateway_id": "", "tag_name": "Stage", "operator": "==", "value": "2"},
        {"gateway_id": "", "tag_name": "Result", "operator": "==", "value": "0"}]
got = resolve_destination(
    [{"enabled": True, "logic": "all", "conditions": both, "output_path": "C:/all"}],
    "C:/fallback", "plain", fetch=fetch)
check("ALL needs every condition", got == "C:/fallback/plain.pdf", got)
got = resolve_destination(
    [{"enabled": True, "logic": "any", "conditions": both, "output_path": "C:/any"}],
    "C:/fallback", "plain", fetch=fetch)
check("ANY needs only one", got == "C:/any/plain.pdf", got)

# ------------------------------------------------- tokens in the path
print()
print("[a tag value can be any part of the folder or the name]")
got = resolve_destination([], "C:/base/{Job}", "{Job}_{Result|1=A,0=B}", fetch=fetch)
check("a tag names the folder AND the file", got == "C:/base/A17/A17_A.pdf",
      "%s - masks let a value print as a word" % got)

# ----------------------------------- the WRITER must use it, not just exist
#
# The first version of this file tested unique_output_path() directly and
# passed with the call removed from render_template_to_pdf - proving the
# function worked while nothing used it. Rendering twice is the only
# version of this check that cannot be fooled.
print()
print("[rendering the same destination twice keeps both reports]")
from app.services.report_renderer import render_template_to_pdf   # noqa: E402

TPL = {"name": "T", "definition": {"sections": [
    {"type": "text", "text": "one"}]}}
target = Path(_TMP) / "render" / "same.pdf"
try:
    a, _, _ = render_template_to_pdf(TPL, output_path=target)
    b, _, _ = render_template_to_pdf(TPL, output_path=target)
    check("the second render does not replace the first",
          a != b and a.exists() and b.exists(),
          "%s vs %s - the writer must apply the rule, not merely have it"
          % (a.name, b.name))
except Exception as exc:                                   # noqa: BLE001
    check("the second render does not replace the first", False, repr(exc))


# ------------------------------------------- the editor keeps what it is given
print()
print("[a template imported from another edge keeps its references]")
DES = io.open(os.path.join(ROOT, "frontend", "src", "components", "Reports",
                           "ReportTemplateDesigner.jsx"), encoding="utf-8").read()
check("the editor collects what a section points at",
      "collectSectionRefs(section)" in DES,
      "a <select> whose value is not in its options renders BLANK, and the "
      "next edit writes the reference away")
check("  gateway pickers show references this edge does not have",
      "gatewayOptions: localGatewayOptions" in DES
      and "FOREIGN_SUFFIX" in DES,
      "shadowing the prop covers all ten pickers at once")
check("  and so do tag pickers", "sectionRefs.tags.get(key)" in DES)
check("  a foreign entry is marked, never silently blended in",
      "not on this edge" in DES)
check("opening a template does not drop keys beside the sections",
      DES.count("...(tpl?.definition || {})") >= 1
      and "definition: { sections: Array.isArray(saved" not in DES,
      "rebuilding the definition as {sections} discards every sibling key")

print()
print("[a full-width pill can have its text centred]")
check("the editor writes title_align", "title_align: e.target.value" in DES,
      "the renderer has always READ title_align for the words inside the "
      "band and defaulted it to left - nothing wrote it, so a full-width "
      "pill could never be centred")
check("  band position is disabled when it cannot do anything",
      'disabled={String(section.pill_fit || "fill") === "fill"}' in DES,
      "a control that silently does nothing is what was reported as broken")

print()
print("[the schedule editor carries the rules]")
SRM = io.open(os.path.join(ROOT, "frontend", "src", "components", "Reports",
                           "ScheduledReportsManager.jsx"), encoding="utf-8").read()
check("the draft carries destination_rules", "destination_rules: []" in SRM,
      "a field missing from the draft is dropped on save")
check("  there is an editor for them", "function DestinationRules(" in SRM)
check("  the destination has its own block, not the cycle grid",
      "Where the file is written" in SRM and 'className="sr-dest-grid"' in SRM)
check("  shown for every trigger mode, not only cycle",
      SRM.index("Where the file is written") > SRM.index("Cycle options"),
      "a time-triggered schedule could not set a destination at all")
# 2026-10-30: the tokens used to be a sentence of <code> under the field.
# They are buttons now, so the operator inserts one instead of transcribing
# it - the complaint was a file literally named {R1_Work_Order_string}.
check("  the run number is offered on screen", '"{n}"' in SRM)
check("  every token is a button, not something to copy by hand",
      '"{date}", "{time}", "{n}", "{template}", "{id}"' in SRM)
check("  the folder and the file name are one aligned grid",
      SRM.count('className="sr-dest-label"') >= 4,
      "labels in their own column is what keeps the fields aligned")
check("  the result is previewed before a run happens",
      "destinationPreview" in SRM,
      "a pattern that becomes a file instead of a folder must be visible "
      "while typing, not after a scheduled run")
# The column editor belongs to the Generated Reports list, not to the
# destination - it was reported as "should not be here at all".
check("  the column editor is not inside the destination block",
      SRM.index("<ReportColumns") > SRM.index("Generated Reports list"))

print()
print("[the store persists them - both statements, or they vanish]")
STORE = io.open(os.path.join(ROOT, "backend", "app", "services",
                             "reports_store.py"), encoding="utf-8").read()
check("destination_rules_json is in the INSERT",
      "destination_rules_json" in STORE.split("INSERT INTO scheduled_reports")[1][:900])
check("  and in the UPDATE", "destination_rules_json = ?" in STORE)
check("  and is read back", '"destination_rules": _json_list' in STORE)
APP_STORE = io.open(os.path.join(ROOT, "backend", "app", "services",
                                 "app_store.py"), encoding="utf-8").read()
check("  and existing installs get the column",
      "destination_rules_json" in APP_STORE,
      "without the migration an upgraded edge fails every schedule save")

# ------------------------------- no cloud configured, no backlog decision
print()
print("[a backlog is only a decision when there is somewhere to sync to]")
APPJSX = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
                 encoding="utf-8").read()
check("the prompt requires a cloud DATABASE, not just a target flag",
      "cloudSyncStatus?.cloud_database_configured" in APPJSX,
      "cloud_target.enabled stays TRUE on an edge with no cloud at all - "
      "the built-in config-sync pseudo-target has no host")
check("  and it closes itself once the backlog drains",
      "BACKLOG_PROMPT_ROWS / 2" in APPJSX,
      "the screenshot showed 118 rows under a heading about a backlog too "
      "large to ignore, because the figures are live")
check("  the dialog says what it was when it fired",
      "backlogPromptAtRows" in APPJSX)
ROUTER = io.open(os.path.join(ROOT, "backend", "app", "routers",
                              "app_store.py"), encoding="utf-8").read()
check("the backend answers the real question",
      "cloud_database_configured" in ROUTER
      and "_get_cloud_database_target()" in ROUTER,
      "it must use the SAME resolver the historian routing uses")

# ------------------------------------------- operator-defined columns
print()
print("[a generated report keeps the values it was generated with]")
SCHED = io.open(os.path.join(ROOT, "backend", "app", "services",
                             "report_scheduler.py"), encoding="utf-8").read()
check("columns are resolved when the report is generated",
      "_resolve_report_columns(schedule, window)" in SCHED,
      "the tag has moved on by the time anyone opens the list")
check("  and stored with the row", '"columns": _resolve_report_columns' in SCHED)
check("  using the SAME token language as the file name",
      "resolve_text_tokens(pattern" in SCHED,
      "a second syntax is a second thing to get wrong")
STORE2 = io.open(os.path.join(ROOT, "backend", "app", "services",
                              "reports_store.py"), encoding="utf-8").read()
check("report_columns survives a save",
      "report_columns_json = ?" in STORE2
      and '"report_columns": _json_list' in STORE2
      and "report_columns_json" in STORE2.split("INSERT INTO scheduled_reports")[1][:950],
      "named in BOTH statements or it saves cleanly and vanishes")
check("  and an upgraded edge gets the column",
      "report_columns_json" in io.open(
          os.path.join(ROOT, "backend", "app", "services", "app_store.py"),
          encoding="utf-8").read())
check("the list shows them", "extraColumns.map" in SRM
      and "g?.meta?.columns" in SRM,
      "read from the ROW, so a report keeps its columns after the schedule "
      "changes")

# --------------------------------- a local user can reach the LAN surfaces
print()
print("[a user created on the edge can open the LAN view]")
import re as _re
# 2026-11-04: the role templates live in _baseRolePermissions; buildRolePermissions
# now adds the catalogue keys on top (ROLE_PERMISSION_ADDITIONS).
_start = APPJSX.index("function _baseRolePermissions(role)") if "function _baseRolePermissions(role)" in APPJSX     else APPJSX.index("function buildRolePermissions(role)")
_body = APPJSX[_start:APPJSX.index(chr(10) + "function ", _start + 10)]


def _flags_for(role):
    key = 'role === "%s"' % role
    if key not in _body:
        return []
    blk = _body[_body.index(key):]
    blk = blk[:blk.index("    };")]
    return [f for f in ("access_full", "access_client", "access_lite") if f in blk]


# can_access_surface() ends at these per-user flags. No role default set any
# of them, so every locally created non-admin was refused at every LAN
# surface - an admin passed only because ADMIN_ROLES short-circuits first.
for role, want in (("admin", "access_full"), ("engineer", "access_full"),
                   ("operator", "access_client"), ("client", "access_client")):
    got = _flags_for(role)
    check("a new '%s' can reach the LAN" % role, want in got,
          "has %s - can_access_surface falls through to these flags"
          % (", ".join(got) or "none"))

_tail = _body[_body.rindex("return {"):]
check("  and so can a role the operator invents",
      "access_client" in _tail,
      "viewer and any custom role land here; the FULL app stays "
      "admin/engineer by role, but read-only must not need a hidden tick")

# ------------------------------- LAN must work on a NEW customer licence
print()
print("[a licence created with defaults can use the LAN]")
CPS = io.open(os.path.join(ROOT, "backend", "app", "services",
                           "control_plane_store.py"), encoding="utf-8").read()
for mod in ("local_web_app", "remote_admin_lan", "lan_access"):
    i = CPS.index('"key": "%s"' % mod)
    entry = CPS[i:CPS.index("}", i)]
    check("'%s' is on by default" % mod, '"default_enabled": True' in entry,
          "can_access_surface() checks the licence BEFORE the admin bypass, "
          "so an edge without this refuses every LAN surface to EVERYONE - "
          "and the middleware turns that into a bare 404")

# ---------------------------- columns are editable where the list is shown
print()
print("[columns can be added from the Generated Reports page]")
check("the generated view has a column editor",
      "setColumnsOpen" in SRM and "saveGlobalColumns" in SRM,
      "the editor was in the SCHEDULE editor, which that page does not render")
check("  it merges instead of replacing app_settings",
      "...current, report_columns: next" in SRM,
      "app_settings holds much more; a partial write would blank it")
SCHED2 = io.open(os.path.join(ROOT, "backend", "app", "services",
                              "report_scheduler.py"), encoding="utf-8").read()
check("  and the scheduler reads the global set",
      'settings.get("report_columns")' in SCHED2,
      "columns defined once must apply to every schedule")

# ------------------------------------- the LAN status must not mislead
print()
print("[the LAN status reports what is actually reachable]")
LANR = io.open(os.path.join(ROOT, "backend", "app", "routers",
                            "lan_sharing.py"), encoding="utf-8").read()
FW_SRC = io.open(os.path.join(ROOT, "backend", "app", "services",
                             "windows_firewall.py"), encoding="utf-8").read()
check("the legacy port field is not faked from the loopback port",
      '"port": http_port,' in LANR and '"port": http_port or primary_port' not in LANR,
      "it reported 8000 - a LOOPBACK-only listener - as the LAN port, so "
      "the operator was handed an http:// address that refuses every call")
check("  reachability is stated per protocol",
      '"http_lan_exposed"' in LANR and '"https_lan_exposed"' in LANR)
check("the firewall check matches the executable PATH",
      "exe_path.replace" in FW_SRC and "in norm" in FW_SRC
      and "_rule_exists_for_path(exe)" in LANR,
      "matching the name 'trustnode' or the bare exe answered present=True "
      "on a machine with NO TrustNode rule - it hit an unrelated python.exe "
      "rule and a 2025 leftover service rule")

# -------------------------------- the columns editor reads the right place
print()
print("[the column editor reads and writes the real settings document]")
check("it reads app_settings from boot.data",
      "boot?.data?.app_settings?.report_columns" in SRM,
      "the bootstrap nests domains under `data`; reading boot.app_settings "
      "returned undefined, which is why the feature did nothing")
check("  and refuses to save when it cannot read them back",
      "nothing else in app_settings can be lost" in SRM,
      "saving {} merged would REPLACE app_settings with just the columns")
check("  the panel has its own layout, not the form grid",
      'className="rc-panel"' in SRM and ".rc-row" in io.open(
          os.path.join(ROOT, "frontend", "src", "styles.css"),
          encoding="utf-8").read(),
      "it borrowed .sr-rule-dest, sized for a narrow form column")

# ------------------------- LAN must work on BOTH installers, LAN and WiFi
print()
print("[remote access: firewall and certificate]")
LANS = io.open(os.path.join(ROOT, "backend", "app", "services",
                            "lan_socket.py"), encoding="utf-8").read()
MAINPY = io.open(os.path.join(ROOT, "backend", "app", "main.py"),
                 encoding="utf-8").read()
TLS = io.open(os.path.join(ROOT, "backend", "app", "services",
                           "lan_tls.py"), encoding="utf-8").read()
FW = io.open(os.path.join(ROOT, "backend", "app", "services",
                          "windows_firewall.py"), encoding="utf-8").read()

check("the firewall result is no longer thrown away",
      "_firewall_state = _fw.ensure_backend_rule()" in LANS
      and "ensure_backend_rule()" in LANS and "except Exception:" in LANS
      and LANS.count("_firewall_state") >= 2,
      "it sat inside an `except Exception: pass`, so the portable build "
      "failed with 'requires elevation' every time and nothing said so")
check("  there is a way to add the rule with elevation",
      "def add_rule_elevated" in FW and "-Verb RunAs" in FW,
      "the installer runs elevated and gets a rule; the portable never can")
check("  it is offered, not forced",
      "Deliberately NOT called automatically" in FW,
      "an unprompted UAC dialog is how an app gets uninstalled")
check("  and exposed to the UI", "/firewall-rule" in LANR)

check("the certificate is re-checked against today's addresses",
      "ensure_certificate_covers_lan" in LANS and "def ensure_certificate_covers_lan" in TLS,
      "SANs are written once at generation; a laptop that later joins WiFi "
      "serves a certificate that does not name its new address, and every "
      "browser on that network refuses outright")

# ------------- the LAN is a VIEW of this edge, so its runtime must match
print()
print("[the LAN listeners run with the same runtime as the edge]")
PLCM = io.open(os.path.join(ROOT, "backend", "app", "services",
                            "plc_manager.py"), encoding="utf-8").read()
check("each LAN loop raises its own thread limiter",
      "current_default_thread_limiter" in LANS and "total_tokens = 64" in LANS,
      "main.py raises it in startup_event, but the LAN servers run with "
      "lifespan=off in their OWN loops and the limiter is a per-loop RunVar - "
      "they served with 40 tokens, which is the 20s save timeout")
check("a subscriber remembers which loop it belongs to",
      "_tn_loop" in PLCM and "def subscribe" in PLCM,
      "a WebSocket accepted by a LAN listener owns a Queue on THAT loop")
check("  and both push paths deliver on that loop",
      PLCM.count("_deliver") >= 3,
      "asyncio.Queue is not thread-safe: put_nowait from the main loop wakes "
      "a future owned by the LAN loop, so a LAN viewer could sit on a stale "
      "chart while the desktop updated live")

# ------------------- remote access must not switch itself on
print()
print("[remote access stays off until someone turns it on]")
check("LAN sharing defaults OFF",
      "LAN_SHARING_DEFAULT = False" in LANS and "def sharing_enabled" in LANS,
      "2026-10-29, from a customer machine: lan_sharing_enabled is absent on "
      "every install made before this was flipped on - not one of the 12 "
      "settings documents in the live store carries it - so defaulting ON "
      "started two listeners, a certificate and a firewall rule needing "
      "elevation on sites that never asked for remote access")
# Behaviour, not spelling: the constant could be renamed and these still hold.
# One dict for globals AND locals: the function looks LAN_SHARING_DEFAULT
# up in its module globals, and splitting them hid it.
_ns = {"Optional": object, "Dict": dict, "Any": object}
exec(compile(LANS[LANS.index("LAN_SHARING_DEFAULT"):
                  LANS.index("def _https_possible")], "lan_socket", "exec"), _ns)
_se = _ns["sharing_enabled"]
check("  an absent setting means off", _se({}) is False and _se(None) is False,
      "this is the key every pre-existing install has")
check("  an operator who turns it ON gets it",
      _se({"lan_sharing_enabled": True}) is True,
      "the 10-27 request is unchanged for anyone who asks for remote access")
check("  and a stored False is still a decision",
      _se({"lan_sharing_enabled": False}) is False)
check("  an operator who turns it off keeps it off",
      'value = s.get("lan_sharing_enabled", None)' in LANS
      and "if value is None:" in LANS,
      "only the ABSENT key may change meaning; a stored False is a decision")
for f, where in (("main.py", MAINPY), ("lan_sharing.py", LANR)):
    check("  %s asks the shared reading" % f, "sharing_enabled(" in where,
          "four call sites each doing their own bool() is how they drift")
check("  nothing reads the raw key any more",
      'bool(s.get("lan_sharing_enabled"))' not in MAINPY
      and 'bool(s.get("lan_sharing_enabled"))' not in LANR)

# ------------------- the administrator can always reach the edge remotely
print()
print("[an admin is not locked out by a seat]")
#
# 2026-10-27, from a customer edge over the LAN, signed in as the admin:
#   {"detail":"Access to this surface is not allowed (seat:studio)"}
# The seat check sits BEFORE the `role in ADMIN_ROLES` bypass, so on a
# licence carrying explicit seats the owner-administrator was refused the
# full runtime until somebody gave them a seat - and the screen that hands
# out seats IS the full runtime. Nobody could ever be first.
import subprocess as _sp
_probe = (
    "import sys; sys.path.insert(0,'.');"
    "from app.services import access_policy as ap;"
    "import app.services.license_inspect as li;"
    "ap.has_module = lambda k: True;"
    "li.seats_are_explicit = lambda: True;"
    "print('|'.join('%s=%s' % (n, ap.surface_access('full', p, True)[0]) for n, p in ("
    "  ('admin',    {'role':'admin','sub':'a','permissions':{}}),"
    "  ('engineer', {'role':'engineer','sub':'e','permissions':{}}),"
    "  ('eng_seat', {'role':'engineer','sub':'e2','permissions':{'seats':['studio'],'access_full':True}}),"
    ")))"
)
_env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1",
            TRUSTNODE_LICENSE_GATES="enforce")
try:
    _out = _sp.run([sys.executable, "-I", "-c", _probe],
                   cwd=os.path.join(ROOT, "backend"), env=_env,
                   capture_output=True, text=True, timeout=180).stdout or ""
    _line = [l for l in _out.splitlines() if "admin=" in l]
    _res = dict(kv.split("=") for kv in (_line[-1].split("|") if _line else []))
except Exception as _exc:                                    # noqa: BLE001
    _res = {}
    print("   probe failed:", _exc)

check("an admin with NO seat may open the full LAN app",
      _res.get("admin") == "True",
      "got %r - the screen that assigns seats is the one being refused"
      % _res.get("admin"))
check("  an engineer without a seat is still refused",
      _res.get("engineer") == "False",
      "got %r - seat counts must still mean something" % _res.get("engineer"))
check("  and an engineer WITH a seat gets in",
      _res.get("eng_seat") == "True", "got %r" % _res.get("eng_seat"))

# ------------------- saving a column must visibly do something
print()
print("[the Generated Reports columns behave as the operator expects]")
check("configured headings show before any new report exists",
      "for (const c of globalColumns || [])" in SRM
      and "globalColumns]" in SRM,
      "reading headings only off the ROWS meant nothing changed until the "
      "next report was generated, so saving looked like it had failed")
check("  a bare tag name is accepted as a tag",
      '"{" not in pattern' in SCHED2,
      "the operator typed SimREAL[3] in a field labelled Value, beside a tag "
      "name, and got that text back")
check("  a pattern that already has tokens is left alone",
      'pattern = "{%s}" % pattern.strip()' in SCHED2
      and 'if "{" not in pattern:' in SCHED2,
      "a mixed pattern like 'WO {Work_Order}' must keep working")

# ------------------ a chart can start where a run started
print()
print("[dashboard chart window from a trigger]")
ROUTER2 = io.open(os.path.join(ROOT, "backend", "app", "routers",
                               "app_store.py"), encoding="utf-8").read()
API = io.open(os.path.join(ROOT, "frontend", "src", "api.js"),
              encoding="utf-8").read()
check("the edge answers WHEN the condition became true",
      "/historian/trigger-window" in ROUTER2 and "def _edge(" in ROUTER2,
      "a rising edge, not 'is it true now' - a start bit held high must "
      "keep the run it began, or every refresh moves the axis")
check("  a stop condition only counts AFTER the start",
      "from_utc=start_at" in ROUTER2,
      "an older stop belongs to the previous run and must not truncate this one")
check("  the UI can ask for it", "getHistorianTriggerWindow" in API)
check("the widget offers the choice",
      '"window_mode"' in APPJSX and "From a trigger" in APPJSX)
check("  rolling count stays the default",
      'current.window_mode || "count"' in APPJSX,
      "an existing widget must be untouched")
check("  charts are bounded by the resolved window",
      "twFromMs" in APPJSX and "triggerWindows[w.id]" in APPJSX)
check("  and fall back when nothing has fired yet",
      "? (triggerWindows[w.id] || null) : null" in APPJSX,
      "an operator must never be shown a blank chart")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
