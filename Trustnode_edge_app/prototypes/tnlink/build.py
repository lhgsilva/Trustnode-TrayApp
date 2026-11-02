# -*- coding: utf-8 -*-
"""Build tnlink-server.exe and tnlink-client.exe.

Asserts its own output: each exe must exist, be newer than the start of this
run, and answer --help when executed. A build that reports success while
leaving a stale artefact behind is indistinguishable from one that worked,
which is how a mis-targeted bundle shipped once already.

    python prototypes/tnlink/build.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
WORK = os.path.join(HERE, "build")
SRC = os.path.join(HERE, "tnlink.py")

# 2026-10-28: "http" and "email" are NOT excluded any more. tnlink_api
# serves the interface on loopback with http.server, which imports email -
# excluding either made the exe a little smaller and the window impossible:
#   interface API not started: No module named 'http'
#   interface API not started: No module named 'email'
# Both only appeared in a console that tray mode hides, so the window
# simply had nothing to talk to and said nothing about why.
EXCLUDE = ["tkinter", "unittest", "pydoc", "doctest",
           "xml", "pdb", "sqlite3", "multiprocessing", "asyncio",
           "distutils", "setuptools", "pip"]

# 2026-10-28, requested: "we need to have one exe app that can be used for
# client and server". Two binaries meant telling a customer WHICH to
# download, and the role was read off the FILE NAME - so renaming the exe
# silently changed what it did. One binary now asks once and remembers;
# --role still overrides, and the old per-role names keep working for
# anyone who already has them.
APP_NAME = "lslink"

#: Two exes, asked for 2026-10-29: "always build two apps exe, one with
#: everything and one that would be used only the browser".
#:
#:   lslink.exe      everything inside, including the Electron window.
#:                   ~140 MB, nothing to install, opens its own window.
#:   lslink-web.exe  the same app without the runtime. ~30 MB, and the
#:                   interface opens in the machine's own browser.
#:
#: Same source, same behaviour: only the window differs, and the small one
#: still falls back to an app-mode Chromium window when one is there.
VARIANTS = (
    ("lslink", True),
    ("lslink-web", False),
)


def _signtool() -> str:
    """The newest x64 signtool.exe from the Windows SDK, or ""."""
    import glob as _g
    base = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    hits = _g.glob(os.path.join(base, "Windows Kits", "10", "bin", "**",
                                "x64", "signtool.exe"), recursive=True)
    return sorted(hits)[-1] if hits else ""


def sign(paths) -> None:
    """Sign the built exes when a certificate is configured.

    2026-10-31, from a customer machine:

        Smart App Control blocked an app that may be unsafe
        We blocked ...\tnlink.exe because we could not verify its publisher

    Same plumbing the edge installer uses (build-release.ps1), so one
    certificate covers both products and neither can ship half-signed: a
    signtool failure stops the build rather than leaving one exe signed and
    the other not.

    Set TRUSTNODE_CSC_LINK to the .pfx and TRUSTNODE_CSC_PASSWORD to its
    password. Without them the build still succeeds and says plainly that
    the result is unsigned - an unsigned build is a legitimate dev build,
    but it must never be a silent one.
    """
    pfx = (os.environ.get("TRUSTNODE_CSC_LINK") or "").strip()
    pwd = os.environ.get("TRUSTNODE_CSC_PASSWORD") or ""
    if not pfx:
        print()
        print("  NOT SIGNED - no TRUSTNODE_CSC_LINK set.")
        print("    Windows will warn about the publisher, and Smart App")
        print("    Control will BLOCK it outright. See docs below.")
        return
    tool = _signtool()
    if not tool:
        raise SystemExit("signtool.exe not found (install the Windows SDK) - "
                         "refusing to ship an unsigned build when a "
                         "certificate was configured")
    for f in paths:
        if not os.path.isfile(f):
            continue
        r = subprocess.run([tool, "sign", "/fd", "SHA256", "/f", pfx,
                            "/p", pwd, "/tr", "http://timestamp.digicert.com",
                            "/td", "SHA256", f],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("signtool failed for %s: %s"
                             % (f, (r.stderr or r.stdout or "").strip()[:300]))
        print("  signed: %s" % f)


def main() -> int:
    started = time.time() - 1
    if not os.path.isfile(SRC):
        print("FAIL: %s is missing" % SRC)
        return 2

    # Stamp this build so a stale exe on someone's desktop can never be
    # mistaken for a fresh one - that cost a round trip on 2026-10-28.
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M")
        with open(os.path.join(HERE, "_build_stamp.py"), "w",
                  encoding="utf-8") as fh:
            fh.write("BUILD_STAMP = " + chr(34) + stamp + chr(34) + chr(10))
        print("  build stamp: %s" % stamp)
    except Exception as exc:
        print("  could not write the build stamp: %r" % exc)

    print("LS-Link - build")
    print("  source: %s" % SRC)
    print()

    # The exe icon IS the tray icon (asked 2026-11-02). Rendered here from the
    # tray's own drawing, so the two can never differ.
    icon_ico = os.path.join(WORK, "lslink.ico")
    icon_png = os.path.join(HERE, "ui", "lslink.png")
    try:
        os.makedirs(WORK, exist_ok=True)
        sys.path.insert(0, HERE)
        import tnlink_tray as _tray
        have_icon = _tray.write_icon_file(icon_ico) and _tray.write_icon_file(icon_png)
    except Exception as exc:                                # noqa: BLE001
        print("  icon NOT rendered: %s" % exc)
        have_icon = False
    print("  exe icon: %s" % ("rendered from the tray drawing" if have_icon else "DEFAULT (no PIL?)"))

    for name, with_electron in VARIANTS:
        print("  building %s.exe ..." % name)
        cmd = [sys.executable, "-m", "PyInstaller", "--onefile", "--console",
               "--name", name, "--distpath", DIST, "--workpath", WORK,
               "--specpath", WORK, "--noconfirm", "--clean"]
        if have_icon:
            cmd += ["--icon", icon_ico]
        # The adapter driver has to travel inside the exe, or the tool is
        # useless on a machine that has never seen it. tap.py looks in
        # sys._MEIPASS first for exactly this reason.
        # The interface travels with the exe, so a machine that has it can
        # show the window and one that does not still gets the console.
        uid = os.path.join(HERE, "ui")
        if os.path.isdir(uid):
            # The FILES, not the folder. `ui/node_modules` holds a 269 MB
            # Electron runtime on a development machine, and adding the
            # directory would have carried all of it into the one exe that
            # is meant to be copied to a plant PC on a memory stick.
            for leaf in ("index.html", "main.js", "preload.js",
                         "package.json", "lslink.png"):
                f = os.path.join(uid, leaf)
                if os.path.isfile(f):
                    cmd += ["--add-data", "%s%sui" % (f, os.pathsep)]
        # The Electron runtime, INSIDE the one file. 2026-10-28, asked
        # for directly: "we need to keep everything in one file for easy
        # movement". It is 269 MB on disk, so the exe stops being small -
        # that cost is the deliberate trade for a file that can be copied
        # to a plant PC on a memory stick and double-clicked.
        edist = os.path.join(HERE, "ui", "node_modules", "electron", "dist")
        if not with_electron:
            print("    browser build - no runtime bundled")
        elif os.path.isdir(edist):
            cmd += ["--add-data", "%s%selectron" % (edist, os.pathsep)]
            print("    bundling the Electron runtime from %s" % edist)
        else:
            print("    NO Electron runtime found - the window will fall back")
            print("    to Edge app mode. Run: cd ui && npm install electron")

        vend = os.path.join(HERE, "vendor")
        if os.path.isdir(vend):
            cmd += ["--add-data", "%s%svendor" % (vend, os.pathsep)]
        # tnlink imports these lazily, so PyInstaller cannot see them by
        # following imports - a missing one only shows as a dead menu on a
        # customer machine. The tray pair is listed for exactly that reason.
        for mod in ("tap", "wintun", "tnlink_bridge", "tnlink_tun",
                    "tnlink_netstack", "tnlink_tray", "tnlink_service", "tnlink_api",
                    "tnlink_identify", "_build_stamp",
                    "pystray", "PIL.Image", "PIL.ImageDraw"):
            cmd += ["--hidden-import", mod]
        for m in EXCLUDE:
            cmd += ["--exclude-module", m]
        cmd.append(SRC)
        r = subprocess.run(cmd, cwd=HERE, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True,
                           errors="replace")
        if r.returncode != 0:
            print(r.stdout[-2500:])
            print("FAIL: PyInstaller returned %d for %s" % (r.returncode, name))
            return 2

    print()
    print("  verifying the artefacts, not the exit code")
    ok = True
    for name, with_electron in VARIANTS:
        exe = os.path.join(DIST, "%s.exe" % name)
        if not os.path.isfile(exe):
            print("    %-20s MISSING" % os.path.basename(exe))
            ok = False
            continue
        st = os.stat(exe)
        fresh = st.st_mtime >= started
        print("    %-20s %6.1f MB  %s  %s"
              % (os.path.basename(exe), st.st_size / 1048576.0,
                 time.strftime("%H:%M:%S", time.localtime(st.st_mtime)),
                 "fresh" if fresh else "STALE - not written by this run"))
        if not fresh:
            ok = False
            continue
        q = subprocess.run([exe, "--selftest-vendor"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True,
                           errors="replace", timeout=120)
        vok = "VENDOR-OK" in (q.stdout or "")
        print("      bundled driver -> %s"
              % ("present" if vok else "MISSING: " + (q.stdout or "")[-90:]))
        if not vok:
            ok = False
        p = subprocess.run([exe, "--help"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True,
                           errors="replace", timeout=120)
        good = p.returncode == 0 and "lslink" in (p.stdout or "").lower()
        print("      --help -> exit %d, %s"
              % (p.returncode, "usable" if good else "BROKEN"))
        if not good:
            print(p.stdout[-600:])
            ok = False

    shutil.rmtree(WORK, ignore_errors=True)
    print()
    if not ok:
        print("RESULT: FAIL")
        return 2
    sign([os.path.join(DIST, "%s.exe" % n) for n, _ in VARIANTS])

    print("RESULT: PASS - %s are in %s"
          % (", ".join("%s.exe" % n for n, _ in VARIANTS), DIST))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
