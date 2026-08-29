# -*- coding: utf-8 -*-
"""Copy the finished installers to every folder listed in build_output_paths.txt.

Runs automatically after `npm run dist` (see desktop/package.json). Also fine to
run on its own against the artefacts already on disk:

    python scripts/copy_build_outputs.py
    python scripts/copy_build_outputs.py --list        # show resolved targets, copy nothing
    python scripts/copy_build_outputs.py --strict      # fail if a target cannot be written

Destinations live in scripts/build_output_paths.txt so adding one later is a
text edit, not a code change.

A copy target is a convenience, not part of the build: if OneDrive is signed
out or a share is down we warn and carry on, because failing a 10-minute build
over an unreachable folder helps nobody. --strict flips that for CI.
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATHS_FILE = os.path.join(ROOT, "scripts", "build_output_paths.txt")
DIST = os.path.join(ROOT, "desktop", "dist")

# What ships. Missing files are reported, not fatal — a portable-only or
# installer-only build is a legitimate thing to copy.
ARTEFACTS = [
    "TrustNode-Setup-{version}.exe",
    "TrustNode-{version}-portable.exe",
]


def app_version() -> str:
    try:
        with open(os.path.join(ROOT, "desktop", "package.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("version") or "0.0.0")
    except Exception:
        return "0.0.0"


def tokens(now: datetime, version: str) -> dict:
    return {
        "{DDMMYY-HHMM}": now.strftime("%d%m%y-%H%M"),
        "{DDMMYY}": now.strftime("%d%m%y"),
        "{HHMM}": now.strftime("%H%M"),
        "{YYYYMMDD}": now.strftime("%Y%m%d"),
        "{YYYY}": now.strftime("%Y"),
        "{MM}": now.strftime("%m"),
        "{DD}": now.strftime("%d"),
        "{HH}": now.strftime("%H"),
        "{mm}": now.strftime("%M"),
        "{VERSION}": version,
    }


def read_destinations(now: datetime, version: str) -> list:
    if not os.path.isfile(PATHS_FILE):
        return []
    out = []
    subs = tokens(now, version)
    with open(PATHS_FILE, encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            for key, val in subs.items():
                line = line.replace(key, val)
            out.append(line.rstrip("\\/"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Copy build outputs to configured folders.")
    ap.add_argument("--list", action="store_true", help="resolve targets and exit")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if any destination fails")
    args = ap.parse_args()

    version = app_version()
    # ONE timestamp for the whole run, so both files land in the same folder
    # even if the copy straddles a minute boundary.
    now = datetime.now()
    dests = read_destinations(now, version)

    print("[build copy] version %s" % version)
    if not dests:
        print("  no destinations configured in %s" % os.path.relpath(PATHS_FILE, ROOT))
        return 0

    files = []
    for pattern in ARTEFACTS:
        name = pattern.format(version=version)
        src = os.path.join(DIST, name)
        if os.path.isfile(src):
            files.append(src)
        else:
            print("  not built, skipping: %s" % name)
    if not files:
        print("  nothing to copy - run `npm run dist` first")
        return 1 if args.strict else 0

    if args.list:
        for d in dests:
            print("  target: %s" % d)
        for f in files:
            print("  file  : %s (%.1f MB)" % (os.path.basename(f),
                                              os.path.getsize(f) / 1048576))
        return 0

    failures = 0
    for dest in dests:
        try:
            os.makedirs(dest, exist_ok=True)
        except Exception as exc:
            print("  SKIP %s -> %s" % (dest, exc))
            failures += 1
            continue
        for src in files:
            name = os.path.basename(src)
            try:
                target = os.path.join(dest, name)
                shutil.copy2(src, target)
                print("  copied %-34s -> %s (%.1f MB)"
                      % (name, dest, os.path.getsize(target) / 1048576))
            except Exception as exc:
                print("  FAILED %s -> %s: %s" % (name, dest, exc))
                failures += 1

    if failures:
        print("  %d copy operation(s) failed" % failures)
        return 2 if args.strict else 0
    print("  all copies complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
