# -*- coding: utf-8 -*-
"""The live-stream error banner must be true, temporary and dismissible.

2026-10-12, reported with a screenshot: a red "Invalid stream payload" bar
across the top of the app, "no way to dismiss this message" - while the
dashboard behind it kept updating with live values.

Three defects, and the message itself was the misleading one:

  1. The try block in ws.onmessage started at JSON.parse and ran for ~285
     lines, wrapping every alarm rule, trigger-state update and setState in
     the handler. A bare `catch` then blamed the PAYLOAD. So an ordinary JS
     fault in the alarm code - a property read on an undefined rule - was
     reported as a malformed packet, sending anyone diagnosing it to the
     backend when the bug was in the browser.

  2. Nothing ever cleared it. One bad packet, or one such bug, left the
     banner up for the rest of the session even though the stream recovered
     on the very next message - which is exactly what the screenshot shows:
     a payload error above a dashboard receiving payloads.

  3. Every other banner on that page can be closed. This one had no control.

Asserted at the source, because the fix is structural: WHICH statements the
parse guard encloses is the whole defect, and that is not observable from a
rendered DOM without provoking an internal fault on purpose.
"""
from __future__ import annotations

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


APP = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
              encoding="utf-8").read()
CSS = io.open(os.path.join(ROOT, "frontend", "src", "styles.css"),
              encoding="utf-8").read()

print("TrustNode - the live-stream error banner")
print()

# The handler under test is the FIRST ws.onmessage (the edge stream); the
# second is the cloud snapshot stream and never raised this banner.
starts = [m.start() for m in re.finditer(r"ws\.onmessage = \(event\) => \{", APP)]
check("the live-stream handler was found", len(starts) >= 1, "%d handlers" % len(starts))
# Up to the NEXT handler, so onclose/onerror are inside the slice too - a
# fixed character window cut them off and made this test fail on code it
# had not touched.
handler = APP[starts[0]:(starts[1] if len(starts) > 1 else len(APP))] if starts else ""

print("[only a parse failure may blame the payload]")
check("the payload message is raised exactly once",
      APP.count('setError("Invalid stream payload")') == 1,
      "found %d" % APP.count('setError("Invalid stream payload")'))

# The parse must sit in its OWN try, whose body is tiny.
m = re.search(r"let data;\s*try \{\s*data = JSON\.parse\(event\.data\);\s*\} catch \(err\) \{",
              handler)
check("JSON.parse has a try of its own", bool(m),
      "it shared one with the entire handler body")
if m:
    # The guard runs from `let data;` to where the long body's own try
    # begins. Measuring to the first "}" stopped inside the log object, and
    # a fixed character window missed the return once the logging was added.
    # Bound it by the body's try instead.
    body_try = handler.index("try {", m.end())
    guard = handler[m.start():body_try]
    check("  that guard is a handful of lines, not hundreds",
          guard.count("\n") < 25,
          "%d lines, against the ~285 it used to share with the body"
          % guard.count("\n"))
    check("  and it RETURNS rather than running the body on bad data",
          "return;" in guard,
          "continuing with `data` undefined would throw again immediately")

# The long body's own catch must not claim the payload is bad. There are two
# `catch (err)` blocks now - the parse guard and the body - so take the LAST,
# which is the body's.
tail = handler[handler.rindex("} catch (err) {"):] if "} catch (err) {" in handler else ""
check("the body's catch reports a HANDLER fault", bool(tail),
      "a bare `catch` cannot tell you what actually failed")
if tail:
    blk = tail[:700]
    check("  it names the real error", "err?.message" in blk,
          "swallowing the message is why this was never diagnosable")
    check("  it logs instead of raising a banner",
          "addAppLog(" in blk and "setError(" not in blk,
          "the stream is healthy; the next packet processes normally")
    check("  and it is categorised as connectivity",
          '"connectivity"' in blk)

print()
print("[a good packet takes the banner down again]")
check("a successful parse clears the error it raised",
      "streamPayloadErrorRef.current = false;" in handler
      and 'setError("");' in handler,
      "the screenshot showed a payload error above a LIVE dashboard")
check("  guarded by a ref, so it clears only OUR banner",
      "if (streamPayloadErrorRef.current) {" in handler,
      "clearing unconditionally would wipe an unrelated error on every "
      "packet, hiding real faults")
check("  the ref is declared", "const streamPayloadErrorRef = useRef(false);" in APP)
check("  and set when the parse fails",
      re.search(r"catch \(err\) \{\s*streamPayloadErrorRef\.current = true;\s*"
                r'setError\("Invalid stream payload"\);', handler) is not None,
      "without this the clear can never fire")
check("  a parse failure records what it could not read",
      "Live stream payload did not parse:" in handler,
      "the banner named a fault and threw away the evidence, which is why a "
      "bad frame and a JS bug were indistinguishable")

print()
print("[the operator can close it]")
check("the banner renders a dismiss control",
      'className="banner-dismiss"' in APP)
check("  which clears the error AND the ref",
      "onClick={() => { streamPayloadErrorRef.current = false; setError(\"\"); }}" in APP,
      "leaving the ref set would stop the next packet clearing a later one")
check("  it is labelled for assistive tech",
      'aria-label="Dismiss"' in APP)
check("  and styled so it sits beside the text, not under it",
      ".banner-dismiss {" in CSS and "display: flex;" in
      CSS[CSS.index(".error {"):CSS.index(".banner-dismiss {")],
      "a block button would push the message around")

print()
print("[nothing that worked has changed]")
check("the cloud-snapshot handler is untouched",
      len(starts) >= 2 and 'JSON.parse(event.data || "{}")' in APP,
      "it has its own guard and never raised this banner")
check("  reconnect on close still happens",
      'setWsState("reconnecting")' in handler and "connect, 1500" in handler)
check("  a socket error still logs and closes",
      'setWsState("disconnected")' in handler and "ws.close();" in handler)
check("  status updates still flow",
      "if (data.status) setStatus(data.status);" in handler)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
