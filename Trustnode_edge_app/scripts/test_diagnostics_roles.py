# -*- coding: utf-8 -*-
"""Every process on the Diagnostics page says what it is for.

2026-10-31, asked for: "more realistic, showing what each role is doing, for
better diagnostics in the future". Nine rows and seven said "desktop app" -
which is why the process burning 74.6% of a core could not be named. Electron
tags every child it spawns on the command line, so the role was recoverable
all along. With this, the same screenshot reads:

    25028  desktop app - app window (UI)        74.6%   <- a renderer
    24640  desktop app (main)
    31912  desktop app - graphics / rendering
    33040  desktop app - network service

    python scripts/test_diagnostics_roles.py
"""
from __future__ import annotations
import os, sys, tempfile

_TMP = tempfile.mkdtemp(prefix="tn_diagrole_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-62s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


class FakeProc:
    def __init__(self, pid, name, argv):
        self.pid, self._n, self._a = pid, name, argv
    def name(self):
        return self._n
    def cmdline(self):
        return self._a


def main() -> int:
    print("diagnostics: every process says what it is for")
    from app.services import diagnostics as d

    cases = [
        (1, "TrustNode.exe", ["TrustNode.exe"], "desktop app (main)"),
        (2, "TrustNode.exe", ["TrustNode.exe", "--type=renderer"],
            "desktop app - app window (UI)"),
        (3, "TrustNode.exe", ["TrustNode.exe", "--type=gpu-process"],
            "desktop app - graphics / rendering"),
        (4, "TrustNode.exe", ["TrustNode.exe", "--type=utility",
                              "--utility-sub-type=network.mojom.NetworkService"],
            "desktop app - network service"),
        (5, "TrustNode.exe", ["TrustNode.exe", "--type=crashpad-handler"],
            "desktop app - crash reporter"),
        (6, "mosquitto.exe", ["mosquitto.exe"], "MQTT broker (sidecar)"),
        (7, "TrustNodeOpcUa.exe", ["x"], "OPC UA server (sidecar)"),
        (8, "conhost.exe", ["conhost.exe"], "console host (Windows)"),
    ]
    for pid, name, argv, want in cases:
        got = d._refine_role(FakeProc(pid, name, argv), "desktop app")
        check("%-26s -> %s" % (name + " " + (argv[1] if len(argv) > 1 else ""), want),
              got == want, "got %r" % got)

    # An unreadable command line must not be cached as a wrong guess.
    class Blind(FakeProc):
        def cmdline(self):
            raise OSError("access denied")
    got = d._refine_role(Blind(99, "TrustNode.exe", []), "desktop app")
    check("an unreadable command line keeps the plain label",
          got == "desktop app", "got %r" % got)
    check("  and is not cached, so a later read can still refine it",
          99 not in d._ROLE_CACHE)

    # The backend keeps its own label - it is not an Electron child.
    got = d._refine_role(FakeProc(10, "trustnode-service.exe", ["x"]), "backend service")
    check("the backend service is left alone", got == "backend service", got)

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
