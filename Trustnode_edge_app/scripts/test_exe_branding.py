# -*- coding: utf-8 -*-
"""The shipped executables must say TrustNode, not Electron.

2026-08-31, from a customer's Task Manager: a 1.8 GB process listed as
"Electron", published by "GitHub, Inc.", version 31.7.7 - and a nameless
helper beside it. Operator: "we should add the trustnode logo as part of the
service and remove electron text and only show trustnode".

Two causes, both now fixed:

  * the Electron shell kept Electron's own version resource, because
    `signAndEditExecutable: false` turns off the rcedit step as well as the
    signing step. (That flag has to stay off: enabling it makes electron-builder
    extract the winCodeSign toolchain, whose macOS symlinks Windows refuses to
    create without elevation, and the build fails outright.) desktop/afterPack.js
    runs rcedit itself instead.
  * the Python service had NO version resource at all, so it showed a blank
    description and publisher. backend/trustnode_version_info.txt supplies one.

This is a shipped-artifact check, so it SKIPS when there is no build on the
machine and FAILS when there is one and it is unbranded - an unbranded build
must not be able to become a release quietly.
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNPACKED = os.path.join(ROOT, "desktop", "dist", "win-unpacked")
SHELL = os.path.join(UNPACKED, "TrustNode.exe")
SERVICE = os.path.join(UNPACKED, "resources", "backend", "trustnode-service.exe")
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:52s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:140]) if detail else ""))
    if not ok:
        FAILS.append(name)


def version_info(path: str) -> dict:
    """FileDescription / ProductName / CompanyName as Windows reports them."""
    ps = (
        "$v = (Get-Item -LiteralPath '%s').VersionInfo; "
        "Write-Output $v.FileDescription; Write-Output $v.ProductName; "
        "Write-Output $v.CompanyName; Write-Output $v.FileVersion" % path
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return {}
    lines = [ln.strip() for ln in (out or "").splitlines()]
    while len(lines) < 4:
        lines.append("")
    return {"FileDescription": lines[0], "ProductName": lines[1],
            "CompanyName": lines[2], "FileVersion": lines[3]}


print("[the shipped executables identify themselves as TrustNode]")
if os.name != "nt":
    print("  SKIP: version resources are a Windows concept")
    sys.exit(0)
if not os.path.exists(SHELL):
    print("  SKIP: no build on this machine (%s)" % SHELL)
    sys.exit(0)

shell = version_info(SHELL)
print("  shell   : %s" % shell)
check("the shell is not branded 'Electron'",
      "electron" not in str(shell.get("FileDescription", "")).lower()
      and "electron" not in str(shell.get("ProductName", "")).lower(),
      "Task Manager shows FileDescription - this is the 'Electron' the "
      "operator saw")
check("  it names TrustNode",
      "trustnode" in str(shell.get("ProductName", "")).lower()
      or "trustnode" in str(shell.get("FileDescription", "")).lower(),
      str(shell))
check("  and is not published by GitHub, Inc.",
      "github" not in str(shell.get("CompanyName", "")).lower(),
      shell.get("CompanyName"))

if os.path.exists(SERVICE):
    svc = version_info(SERVICE)
    print("  service : %s" % svc)
    check("the service carries a version resource",
          bool(str(svc.get("FileDescription", "")).strip()),
          "an empty description shows as a nameless helper in Task Manager")
    check("  and names TrustNode",
          "trustnode" in str(svc.get("FileDescription", "")).lower()
          or "trustnode" in str(svc.get("ProductName", "")).lower(),
          str(svc))
else:
    print("  (service exe not present in this build tree)")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
