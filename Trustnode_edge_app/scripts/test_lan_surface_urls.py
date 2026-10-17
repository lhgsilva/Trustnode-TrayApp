# -*- coding: utf-8 -*-
"""LAN sharing offers a name, not an IP - and the third surface is Web Lite.

2026-10-17, asked: "what would be the other options than give users IPs for
the lan sharing? can we create a url more user friendly?" and, in the same
message, "we would have the full, read only and lite (legacy that should
called Web Lite)".

The friendlier URL already existed. `/api/lan-sharing/status` has returned
`hostname_urls` for HTTP and HTTPS all along - the tray menu just never
rendered them, so every operator was handed a raw 192.168.x.x to type into a
phone. The machine name also survives a DHCP lease change, which an IP does
not.

Read-only: inspects source. No store, no network.
"""
from __future__ import annotations

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


def read(*parts):
    return io.open(os.path.join(ROOT, *parts), encoding="utf-8").read()


print("TrustNode - a LAN address someone can type, on three named surfaces")
print()

print("[1. the backend offers a hostname, not only IPs]")
api = read("backend", "app", "routers", "lan_sharing.py")
check("status returns hostname_urls over http", '"hostname_urls"' in api)
check("  and over https",
      api.count('"hostname_urls"') >= 2,
      "the HTTPS block needs its own: the ports differ")
check("  built from the machine name",
      "socket.gethostname()" in api)

print()
print("[2. the tray shows the name FIRST]")
tray = read("desktop", "main.js")
check("the tray reads hostname_urls", "hostname_urls" in tray,
      "the backend computed these all along and nobody rendered them")
check("  for https too", "hostUrlsHttps" in tray or "https.hostname_urls" in tray)
# Order is the whole point: a name the operator can type has to come before
# the IP fallback, or nothing changes for them.
_fn = tray[tray.index("const surfaceItems ="):]
_fn = _fn[:_fn.index("const liteSubmenu")]
# Read the ORDER OF THE CALLS, not the parameter list - the first version of
# this check matched the signature `(label, httpList, httpsList, hostHttp,
# hostHttps)` and failed while the body was already correct.
import re as _re                                                  # noqa: E402
_order = _re.findall(r"\(\s*(host\w+|http\w+)\s*\|\|\s*\[\]\s*\)\.forEach", _fn)
check("  every address list is actually rendered", len(_order) == 4, _order)
check("  hostname URLs are rendered before the IPs",
      len(_order) == 4 and all(n.startswith("host") for n in _order[:2]),
      "render order is %s - an IP first means the operator still copies "
      "the IP" % (_order,))
check("  and a duplicate address is not listed twice",
      "seen" in _fn and "Set()" in _fn,
      "the hostname and an IP can resolve to the same URL string")

print()
print("[3. the three surfaces are named for what they are]")
check("the tray calls the third surface Web Lite",
      '"Web Lite:"' in tray, "it was 'Lite (legacy read-only)'")
check("  the full runtime is still named", "full runtime" in tray)
check("  and the read-only view is still named", "read-only" in tray)

print()
print("[4. no operator-facing 'legacy' label survives]")
SURFACES = [
    ("frontend", "src", "App.jsx"),
    ("frontend", "src", "components", "RemoteAccess", "RemoteAccessPage.jsx"),
    ("backend", "app", "services", "permission_catalog.py"),
    ("backend", "static", "lite", "index.html"),
    ("backend", "static", "login", "index.html"),
]
for parts in SURFACES:
    txt = read(*parts)
    bad = [m for m in ("Lite (legacy", "legacy Lite page", "legacy read-only")
           if m in txt]
    check("  %-46s" % parts[-1], not bad,
          "still shows %s" % bad)

print()
print("[5. the KEY is untouched - only the label changed]")
# Every permission, view link and route is built on `lite`. Renaming the key
# would break saved links and stored permissions; only the words change.
perms = read("backend", "app", "services", "permission_catalog.py")
check('the permission key is still "access_lite"',
      '"key": "access_lite"' in perms,
      "renaming the key would revoke it for everyone who already has it")
check("  the route is still /trustnode/lite/",
      "/trustnode/lite/" in read("frontend", "src", "App.jsx")
      or "trustnode/lite" in tray,
      "saved view links carry this path")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
