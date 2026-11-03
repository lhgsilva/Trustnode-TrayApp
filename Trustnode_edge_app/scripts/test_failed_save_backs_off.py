# -*- coding: utf-8 -*-
"""A configuration save that failed is not re-sent unchanged every tick.

2026-11-03, from a customer edge: "Devices was NOT saved: Timed out after
20s" for six domains, every ten to twenty seconds, while nobody edited
anything. Each saver records its signature only on SUCCESS, so after one
failure every state tick (the gateway status poll refreshes the array
every 10 s) sent the same payload again. Six domains, one request each per
tick, against an edge that was already busy - the storm that kept it busy.

Now a payload that has just failed waits SAVE_RETRY_BACKOFF_MS before it is
sent again; a CHANGED payload goes at once. Checked on the source (every
saver, not one) and by running the helper's own logic under node.

    python scripts/test_failed_save_backs_off.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(ROOT, "frontend", "src", "App.jsx")
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-70s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def main() -> int:
    print("a failed configuration save backs off")
    src = open(APP, encoding="utf-8").read()
    sig_lines = re.findall(r"if \(signature === (\w+LastPersistSignatureRef)\.current\) return;\n\s*(.*)", src)
    check("every saver (8) compares a signature before saving", len(sig_lines) == 8, "%d" % len(sig_lines))
    guarded = [r for r, nxt in sig_lines if ('saveRecentlyFailed("%s", signature)' % r) in nxt]
    check("  and each one skips a payload that has just failed",
          len(guarded) == 8, "guarded: %s" % sorted(set(r for r, _ in sig_lines) - set(guarded)))
    noted = re.findall(r'noteSaveFailure\("(\w+LastPersistSignatureRef)", signature\)', src)
    check("  and each one records the failure with that signature",
          sorted(set(noted)) == sorted(set(r for r, _ in sig_lines)),
          "missing: %s" % sorted(set(r for r, _ in sig_lines) - set(noted)))
    m = re.search(r"const SAVE_RETRY_BACKOFF_MS = (\d+);", src)
    check("the back-off is a minute, not seconds and not for ever",
          bool(m) and 30000 <= int(m.group(1)) <= 300000, m.group(0) if m else "missing")

    # Run the helper logic itself under node, lifted from the source.
    helpers = re.search(r"(const saveFailureRef = useRef\(\{\}\);.*?\n  \};\n  const saveRecentlyFailed = .*?\n  \};)", src, re.S)
    check("the helpers are in the source", bool(helpers))
    js = (helpers.group(1) if helpers else "").replace("useRef({})", "{ current: {} }")
    script = f"""
let now = 1000000; const Date = {{ now: () => now }};
const SAVE_RETRY_BACKOFF_MS = {m.group(1) if m else 60000};
{js}
const out = [];
out.push(saveRecentlyFailed("k", "sigA"));            // nothing failed yet -> false
noteSaveFailure("k", "sigA");
out.push(saveRecentlyFailed("k", "sigA"));            // same payload, just failed -> true
out.push(saveRecentlyFailed("k", "sigB"));            // a changed payload -> false
out.push(saveRecentlyFailed("other", "sigA"));        // another domain -> false
now += SAVE_RETRY_BACKOFF_MS + 1;
out.push(saveRecentlyFailed("k", "sigA"));            // a minute later -> false
console.log(JSON.stringify(out));
"""
    try:
        res = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        got = res.stdout.strip()
    except Exception as exc:
        got = "node failed: %r" % exc
    check("the helper: skip only the same payload, only within the window",
          got == "[false,true,false,false,false]", got)

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
