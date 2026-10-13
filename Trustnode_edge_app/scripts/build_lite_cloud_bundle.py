# -*- coding: utf-8 -*-
"""Build web_cloud_readonly/lite/ - the cloud copy of the phone app.

The LAN app (backend/static/lite_view/) and the cloud app are the SAME
source. They differ in exactly one line: the importmap either points
@supabase/supabase-js at the local read-only shim, or at the real library.
Keeping one source and swapping that line is why a fix made for the phone on
LAN reaches the phone on cloud.

config.json is MERGED onto whatever the VPS is already serving, never
invented: it carries the Supabase URL, the anon key and the tenant, and
inventing those would take the deployment down. Only the keys named in
--enable-tab are changed.

ORDER MATTERS: scripts/build-release.ps1 recreates web_cloud_readonly/ and
does NOT produce lite/, so a release build DELETES this bundle. Run this
script AFTER the release build, never before, or you will push whatever
survived - which on 2026-09-30 was nothing at all.

Usage:
  python scripts/build_lite_cloud_bundle.py                 # merge live config
  python scripts/build_lite_cloud_bundle.py --enable-tab reporting --enable-tab history
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "backend" / "static" / "lite_view"
OUT = ROOT / "web_cloud_readonly" / "lite"
LIVE_CONFIG = "https://trustnode.lsapps.app/lite/config.json"

REAL_SUPABASE = "https://esm.sh/@supabase/supabase-js@2.45.4"
SHIM = "./local_supabase_shim.js"

# Copied verbatim. local_supabase_shim.js is deliberately NOT here: it is the
# LAN adapter, and shipping it to the cloud would be the whole bug in reverse.
COPY = ["styles.css", "manifest.json", "sw.js",
        "trustnode_app_icon.png", "trustnode_app_icon_180.png"]

TAB_KEYS = {
    "dashboard": "show_dashboard_tab", "power": "show_power_tab",
    "alarms": "show_alarms_tab", "reporting": "show_reporting_tab",
    "history": "show_history_tab", "tags": "show_tags_tab",
    "batch": "show_batch_tab", "intelligence": "show_intelligence",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--enable-tab", action="append", default=[],
                    choices=sorted(TAB_KEYS), help="turn this tab on in config.json")
    ap.add_argument("--config-from", default=LIVE_CONFIG,
                    help="URL of the config.json to merge onto")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)

    html = (SRC / "index.html").read_text(encoding="utf-8")
    if SHIM not in html:
        print("FAIL: the LAN importmap entry was not found - has index.html changed?")
        return 2
    html = html.replace('"@supabase/supabase-js": "%s"' % SHIM,
                        '"@supabase/supabase-js": "%s"' % REAL_SUPABASE, 1)
    # The importmap entry, not the word: a comment above it explains the
    # LAN/cloud split and mentioning the filename there is correct.
    if '"@supabase/supabase-js": "%s"' % SHIM in html:
        print("FAIL: the importmap still points at the shim after the swap")
        return 2
    (OUT / "index.html").write_text(html, encoding="utf-8", newline="")
    print("  + index.html         (cloud importmap, %d lines)" % html.count("\n"))

    for name in COPY:
        src = SRC / name
        if src.is_file():
            shutil.copy2(src, OUT / name)
            print("  + %-18s (%d bytes)" % (name, src.stat().st_size))
        else:
            print("  - %-18s (absent locally, VPS copy kept)" % name)

    try:
        with urllib.request.urlopen(args.config_from, timeout=30) as r:
            cfg = json.loads(r.read().decode("utf-8"))
        print("  = config.json merged onto the live one (%d keys)" % len(cfg))
    except Exception as exc:
        print("FAIL: could not read the live config (%s)." % str(exc)[:80])
        print("      Refusing to invent one - it holds the Supabase URL and anon key.")
        return 2

    for tab in args.enable_tab:
        key = TAB_KEYS[tab]
        cfg[key] = True
        print("      %s = true" % key)
    (OUT / "config.json").write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="")

    print("\nBundle ready at %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
