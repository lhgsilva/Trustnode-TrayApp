# -*- coding: utf-8 -*-
"""A finished build must land in every folder listed in build_output_paths.txt.

Requested 2026-08-26: "when we are rebuild the exes I also want a copy of the
exes to a different folder, I want the path to be located from a txt file so we
can change or add new locations in the future."

The point of the txt file is that a new destination is a text edit, never a code
change - so these tests drive the REAL script with a temporary paths file and
throwaway artefacts, and check the contract an operator depends on:

  * every configured folder receives both installers;
  * {DDMMYY-HHMM} resolves to a real timestamp folder;
  * several destinations all get a copy;
  * a destination that cannot be written WARNS but does not fail the build
    (OneDrive signed out, share down) - unless --strict is asked for;
  * comments and blank lines in the file are ignored.
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "copy_build_outputs.py")
PATHS = os.path.join(ROOT, "scripts", "build_output_paths.txt")
DIST = os.path.join(ROOT, "desktop", "dist")
NAMES = ["TrustNode-Setup-0.1.0.exe", "TrustNode-0.1.0-portable.exe"]
FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:110]) if detail else ""
    print("  {0:56s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


def run(paths_text, extra_args=()):
    """Run the real copier with a temporary destinations file."""
    backup = io.open(PATHS, encoding="utf-8").read() if os.path.isfile(PATHS) else ""
    io.open(PATHS, "w", encoding="utf-8").write(paths_text)
    try:
        return subprocess.run([sys.executable, SCRIPT] + list(extra_args),
                              capture_output=True, text=True, cwd=ROOT)
    finally:
        io.open(PATHS, "w", encoding="utf-8").write(backup)


# throwaway artefacts so a real build is never required
made_dist = not os.path.isdir(DIST)
os.makedirs(DIST, exist_ok=True)
created = []
for n in NAMES:
    f = os.path.join(DIST, n)
    if not os.path.isfile(f):
        io.open(f, "wb").write(b"x" * 4096)
        created.append(f)

tmp = tempfile.mkdtemp(prefix="tn-copyspec-")
try:
    # --- one destination, timestamped -------------------------------------
    print("[a timestamped destination]")
    d1 = os.path.join(tmp, "one")
    r = run("# a comment\n\n" + os.path.join(d1, "{DDMMYY-HHMM}") + "\n")
    check("the copier succeeds", r.returncode == 0, r.stdout.strip()[-90:])
    subs = os.listdir(d1) if os.path.isdir(d1) else []
    check("  it created the dated folder", len(subs) == 1, subs)
    if subs:
        check("  named DDMMYY-HHMM", bool(re.fullmatch(r"\d{6}-\d{4}", subs[0])), subs[0])
        got = sorted(os.listdir(os.path.join(d1, subs[0])))
        check("  with BOTH installers", got == sorted(NAMES), got)
        # compare against the SOURCE, not a fixed size: dist may hold the real
        # 184 MB installers rather than this test's throwaway stand-ins
        src_sz = os.path.getsize(os.path.join(DIST, NAMES[0]))
        sz = os.path.getsize(os.path.join(d1, subs[0], NAMES[0]))
        check("  copied whole, not truncated", sz == src_sz,
              "{0} vs source {1}".format(sz, src_sz))

    # --- several destinations --------------------------------------------
    print("\n[several destinations]")
    d2, d3 = os.path.join(tmp, "two"), os.path.join(tmp, "three")
    r = run("\n".join([d2, os.path.join(d3, "{DDMMYY}")]) + "\n")
    check("all configured folders receive the build",
          os.path.isdir(d2) and os.path.isdir(d3) and r.returncode == 0)
    check("  a plain folder gets the files directly",
          sorted(os.listdir(d2)) == sorted(NAMES), os.listdir(d2))
    check("  {DDMMYY} resolves on its own",
          bool(os.listdir(d3)) and re.fullmatch(r"\d{6}", os.listdir(d3)[0]),
          os.listdir(d3))

    # --- comments and blanks ---------------------------------------------
    print("\n[the file format]")
    d4 = os.path.join(tmp, "four")
    r = run("# only a comment\n\n   \n" + d4 + "\n# trailing comment\n")
    check("comments and blank lines are ignored",
          os.path.isdir(d4) and sorted(os.listdir(d4)) == sorted(NAMES),
          os.listdir(d4) if os.path.isdir(d4) else "(missing)")

    # --- an unreachable destination --------------------------------------
    print("\n[an unreachable destination]")
    bad = "\\\\no-such-host-xyz\\builds\\{DDMMYY-HHMM}"
    good = os.path.join(tmp, "five")
    r = run(bad + "\n" + good + "\n")
    check("a build is NOT failed by an unreachable folder", r.returncode == 0,
          "exit={0}".format(r.returncode))
    check("  and it says which one it skipped",
          "SKIP" in r.stdout or "FAILED" in r.stdout, r.stdout.strip()[-90:])
    check("  while the reachable one still gets the build",
          os.path.isdir(good) and sorted(os.listdir(good)) == sorted(NAMES),
          os.listdir(good) if os.path.isdir(good) else "(missing)")
    r = run(bad + "\n", ("--strict",))
    check("  --strict DOES fail, for CI", r.returncode != 0,
          "exit={0}".format(r.returncode))

    # --- the shipped configuration ---------------------------------------
    print("\n[the configuration that ships]")
    txt = io.open(PATHS, encoding="utf-8").read()
    live = [ln.strip() for ln in txt.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    check("build_output_paths.txt has at least one destination", bool(live), live)
    check("  and it is documented for whoever edits it next",
          txt.count("#") >= 5 and "{DDMMYY-HHMM}" in txt)
    check("  the OneDrive destination is configured",
          any("OneDrive" in ln for ln in live), live[:2])

    # --- wired into the build --------------------------------------------
    pkg = io.open(os.path.join(ROOT, "desktop", "package.json"), encoding="utf-8").read()
    check("`npm run dist` runs the copy step", "copy:outputs" in pkg
          and "electron-builder --win nsis portable && npm run copy:outputs" in pkg)
finally:
    for f in created:
        try:
            os.remove(f)
        except Exception:
            pass
    if made_dist:
        shutil.rmtree(DIST, ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
