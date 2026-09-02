# -*- coding: utf-8 -*-
"""No hook may depend on a value declared later in the same component.

2026-08-27: the app died on load with

    Frontend Error Recovered
    Cannot access 'Ue' before initialization

A useEffect was inserted above the `const [activePage] = useState(...)` it
depended on. A dependency array is evaluated DURING RENDER, so referencing a
const that has not been initialised yet throws a ReferenceError before the
component can mount - the whole UI is gone, not one widget. Minification
renames the variable, so the message names 'Ue' and tells you nothing.

`npm run build` does NOT catch this: it is a runtime temporal-dead-zone error,
and the bundle compiles perfectly. Every static check I had passed too. Hence
this test - it is the only thing standing between that mistake and a shipped
build.

Scope: hook dependency arrays in the frontend, checked against the line where
each identifier is declared in the same file. Deliberately conservative - it
only reports an identifier it can see being declared LATER at component scope.
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "frontend", "src")
FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:150]) if detail else ""
    print("  {0:56s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


def jsx_files():
    out = []
    for dirpath, _, files in os.walk(SRC):
        if "node_modules" in dirpath:
            continue
        for f in files:
            if f.endswith((".jsx", ".js")):
                out.append(os.path.join(dirpath, f))
    return sorted(out)


# `}, [a, b, c]);` - the closing line of a hook with a BRACED body
DEPS = re.compile(r"^\s*\}\s*,\s*\[([^\]]*)\]\s*\)\s*;")
# `), [a, b]);` / `value, [a]);` - a hook whose arrow body has no braces. The
# 2026-08-31 OEE regression looked like this, and DEPS above cannot see it.
TAIL = re.compile(r",\s*\[([^\]]*)\]\s*\)\s*;\s*$")
# the line that OPENS a hook call
HOOK_OPEN = re.compile(r"(?:=\s*|^\s*)use[A-Z][A-Za-z]*\s*\(")
# component-scope declarations we can locate reliably
DECL = re.compile(
    r"^\s*const\s+(?:\[\s*(?P<destructured>[A-Za-z0-9_,\s]+?)\s*\]|(?P<plain>[A-Za-z_$][\w$]*))\s*="
)
IDENT = re.compile(r"^[A-Za-z_$][\w$]*$")
# a component/function starting at column 0 - the scope a hook belongs to
TOPLEVEL = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:function\s+[A-Za-z_$][\w$]*|"
    r"const\s+[A-Za-z_$][\w$]*\s*=\s*(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>|"
    r"class\s+[A-Za-z_$][\w$]*)")

print("[hook dependencies must be declared before the hook]")
offenders = []
scanned = 0

for path in jsx_files():
    text = io.open(path, encoding="utf-8", errors="replace").read()
    lines = text.split("\n")

    # Component boundaries. A `series` declared in one component and a hook in
    # ANOTHER are unrelated - comparing across them produced false alarms, and
    # a test that cries wolf gets ignored. Top-level declarations (column 0)
    # start a new block.
    bounds = [i for i, ln in enumerate(lines)
              if TOPLEVEL.match(ln)]
    bounds.append(len(lines))

    def block_of(idx):
        lo = 0
        for b in bounds:
            if b <= idx:
                lo = b
            else:
                return lo, b
        return lo, len(lines)

    # first declaration line per name, per block
    declared_at = {}
    for i, ln in enumerate(lines):
        m = DECL.match(ln)
        if not m:
            continue
        blk = block_of(i)[0]
        if m.group("destructured"):
            names = [n.strip() for n in m.group("destructured").split(",")]
        else:
            names = [m.group("plain")]
        for n in names:
            if n and IDENT.match(n) and (blk, n) not in declared_at:
                declared_at[(blk, n)] = i

    def opens_a_hook(idx):
        """Is line `idx` the tail of a use*() call?

        Walks back to the start of the statement - the previous line that
        ended in `;` - and looks for the hook call on the way. Bounded, so a
        plain function call whose last argument happens to be an array is not
        reported.
        """
        for j in range(idx, max(-1, idx - 30), -1):
            if HOOK_OPEN.search(lines[j]):
                return True
            if j < idx and lines[j].rstrip().endswith(";"):
                return False        # previous statement: we have gone too far
        return False

    for i, ln in enumerate(lines):
        m = DEPS.match(ln)
        if not m:
            m = TAIL.search(ln)
            if not m or not opens_a_hook(i):
                continue
        scanned += 1
        blk = block_of(i)[0]
        for raw in m.group(1).split(","):
            name = raw.strip()
            # only bare identifiers; `a.b`, calls and literals cannot TDZ here
            if not name or not IDENT.match(name):
                continue
            at = declared_at.get((blk, name))
            if at is not None and at > i:
                offenders.append(
                    (os.path.relpath(path, SRC).replace("\\", "/"), i + 1, name, at + 1))

print("  dependency arrays scanned                               : {0}".format(scanned))
check("no hook depends on a later declaration", not offenders,
      "; ".join("{0}:{1} uses '{2}' declared at line {3}".format(*o)
                for o in offenders[:3]))

if offenders:
    print()
    print("  Each of these throws 'Cannot access X before initialization' on load:")
    for f, use_line, name, decl_line in offenders:
        print("    {0}:{1}  depends on '{2}', declared at line {3}".format(
            f, use_line, name, decl_line))

# ---------------------------------------------------------------------------
# useState read before it is declared
# ---------------------------------------------------------------------------
# The dependency-array check above only sees `}, [deps]);` lines. 2026-08-27 I
# made the same mistake in a plain render expression instead:
#
#     const shown = points.filter((d) => channelFilter === "all" || ...)
#     ...
#     const [channelFilter, setChannelFilter] = useState("all");   // BELOW it
#
# That throws "Cannot access 'X' before initialization" the moment the
# component renders, exactly like the shipped regression, and no dependency
# array is involved.
#
# `const [x, setX] = useState(...)` is unambiguous enough to check directly:
# using x above its own declaration in the same component is ALWAYS wrong.
print()
print("[no useState value is read above its own declaration]")
STATE = re.compile(
    r"^\s*const\s*\[\s*([A-Za-z_$][\w$]*)\s*,\s*[A-Za-z_$][\w$]*\s*\]\s*=\s*useState")
early = []
for path in jsx_files():
    lines = io.open(path, encoding="utf-8", errors="replace").read().split("\n")
    bounds = [i for i, ln in enumerate(lines) if TOPLEVEL.match(ln)]
    bounds.append(len(lines))

    def block_start(idx):
        lo = 0
        for b in bounds:
            if b <= idx:
                lo = b
            else:
                break
        return lo

    for i, ln in enumerate(lines):
        m = STATE.match(ln)
        if not m:
            continue
        name = m.group(1)
        blk = block_start(i)
        word = re.compile(r"\b" + re.escape(name) + r"\b")
        # An object KEY (`devices: []`) and a string literal ("generated")
        # are not reads of the variable. Both produced false alarms on the
        # first run of this check, and a test that cries wolf gets ignored.
        keyish = re.compile(r"(^|[,{(])\s*" + re.escape(name) + r"\s*:")
        strings = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`")
        for j in range(blk, i):
            text = lines[j]
            stripped = text.strip()
            # skip comments - a name in prose is not a read
            if stripped.startswith(("//", "*", "/*")):
                continue
            text = strings.sub("''", text)
            text = keyish.sub(r"\1", text)
            if word.search(text):
                early.append((os.path.relpath(path, SRC).replace("\\", "/"),
                              j + 1, name, i + 1))
                break

check("no state value is used above its useState", not early,
      "; ".join("{0}:{1} uses '{2}' declared at {3}".format(*e) for e in early[:3]))
if early:
    print()
    print("  Each of these throws on first render:")
    for f, use_line, name, decl_line in early:
        print("    {0}:{1}  reads '{2}', declared at line {3}".format(
            f, use_line, name, decl_line))

# the specific regression, named so it cannot quietly come back
print()
print("[the 2026-08-27 regression specifically]")
app = io.open(os.path.join(SRC, "App.jsx"), encoding="utf-8", errors="replace").read()
app_lines = app.split("\n")
try:
    active_at = next(i for i, ln in enumerate(app_lines)
                     if ln.startswith("  const [activePage, setActivePage]"))
except StopIteration:
    active_at = -1
mobile_users = [i for i, ln in enumerate(app_lines)
                if ln.strip() == "}, [useMobileLayout, activePage]);"]
check("activePage is declared in App.jsx", active_at >= 0, active_at + 1)
check("  the mobile-table effect sits BELOW it",
      bool(mobile_users) and all(u > active_at for u in mobile_users),
      "declared line {0}, used at {1}".format(
          active_at + 1, [u + 1 for u in mobile_users]))

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
