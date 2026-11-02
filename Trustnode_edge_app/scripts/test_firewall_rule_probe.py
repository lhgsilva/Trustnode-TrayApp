# -*- coding: utf-8 -*-
"""The firewall probe must answer about the rule that is actually there.

2026-10-30, from the Remote Access page on a working install: it warned
"no inbound rule allows this program - other computers cannot reach it"
while the rule existed, was enabled, and named the running exe exactly.

Measured, same machine, same rule:

    plain    rc=0 bytes= 610  has 'Program:'=False  path found=False
    verbose  rc=0 bytes= 950  has 'Program:'=True   path found=True

`netsh advfirewall firewall show rule name=X` does not print the Program
line at all. The probe searched that output for the exe path, so it could
only ever return False. Two things broke, and the second is the expensive
one:

  1. the page cried wolf about a LAN that was fine;
  2. ensure_backend_rule() uses this same probe to decide whether the rule
     is already there - so it re-ran `add rule` on every single start.
     Elevated, that appends a duplicate each time. A machine in the field
     had accumulated 247 rules before anyone looked.

This checks the real netsh, not a recorded string, because the bug WAS in
what netsh prints. On a machine with no TrustNode rule the functional half
is skipped and says so rather than passing silently.

    python scripts/test_firewall_rule_probe.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL, SKIP = [], [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-62s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if detail else ""))


def skip(name, why):
    SKIP.append(name)
    print("  %-62s SKIP  - %s" % (name, why))


def main() -> int:
    print("windows firewall rule probe")
    from app.services import windows_firewall as fw

    # The flag itself. Cheap, and it fails the moment someone drops it.
    src = subprocess.list2cmdline([])  # keep flake8 quiet about the import
    del src
    import inspect
    body = inspect.getsource(fw._rule_exists_for_path)       # noqa: SLF001
    check("the probe asks netsh for verbose output",
          '"verbose"' in body or "'verbose'" in body,
          "without it netsh never prints the Program line")

    if not fw._is_windows():                                 # noqa: SLF001
        skip("the probe finds a rule that really exists", "not windows")
        skip("and does not claim a rule for some other exe", "not windows")
    else:
        # Ask netsh directly what the rule says, then make the probe agree.
        try:
            proc = subprocess.run(
                ["netsh", "advfirewall", "firewall", "show", "rule",
                 "name=%s" % fw._RULE_NAME, "verbose"],       # noqa: SLF001
                capture_output=True, text=True, timeout=10)
            out = (proc.stdout or "") + (proc.stderr or "")
        except Exception as exc:                             # noqa: BLE001
            out = ""
            print("   (netsh unavailable: %s)" % exc)

        m = re.search(r"^Program:\s*(.+?)\s*$", out, re.MULTILINE)
        if not m:
            skip("the probe finds a rule that really exists",
                 "no '%s' rule on this machine" % fw._RULE_NAME)
            skip("and does not claim a rule for some other exe",
                 "nothing to compare against")
        else:
            real = m.group(1)
            check("the probe finds a rule that really exists",
                  fw._rule_exists_for_path(real) is True,    # noqa: SLF001
                  "netsh says Program: %s" % real)
            # The other half: it must not simply say yes to everything,
            # or the first check passes for the wrong reason.
            bogus = r"C:\does\not\exist\nothing-here.exe"
            check("and does not claim a rule for some other exe",
                  fw._rule_exists_for_path(bogus) is False,  # noqa: SLF001
                  "a probe that always says True hides a missing rule")

    print()
    print("  %d passed, %d failed, %d skipped" % (len(PASS), len(FAIL), len(SKIP)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
