# -*- coding: utf-8 -*-
"""A live-stream frame must be JSON a browser can parse.

2026-10-12, reported: a permanent "Invalid stream payload" banner that came
straight back after being dismissed, over a dashboard showing live values and
an EMPTY chart. Returning every packet means the parse is failing every
packet, not once.

json.dumps with Python's defaults emits bare `NaN`, `Infinity` and
`-Infinity` for non-finite floats. None of the three is valid JSON (RFC 8259
has no such literals), so JSON.parse throws on the ENTIRE frame - the browser
cannot skip one bad reading and keep the rest. A single faulted analog
channel therefore kills the live stream for every tag on that gateway, which
is exactly the shape of the report: values that arrive by other routes still
render, the chart that is fed by the stream never fills.

allow_nan=False is not the fix on its own: it raises, which moves the failure
to the server and drops the frame entirely. The reading is genuinely unknown,
so it is sent as null - already how a missing value is represented everywhere
else in the payload.

Reads no live data and starts no server.
"""
from __future__ import annotations

import io
import json
import math
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


print("TrustNode - live-stream frames are parseable JSON")
print()

MAIN = io.open(os.path.join(ROOT, "backend", "app", "main.py"),
               encoding="utf-8").read()

# Lift _ws_json out and run it, so this exercises the SHIPPING helper rather
# than a copy that could drift from it.
start = MAIN.index("def _ws_json(payload: object) -> str:")
end = MAIN.index('@app.websocket("/ws/stream")')
ns: dict = {"json": json, "math": math}
exec(compile(MAIN[start:end], "ws_json_block", "exec"), ns)      # noqa: S102
_ws_json = ns["_ws_json"]

print("[what Python emits by default is not JSON]")
bad = json.dumps({"value": float("nan")})
check("json.dumps writes a bare NaN", "NaN" in bad, bad)
def _strict_loads(text):
    """json.loads with the leniency switched off.

    Python ACCEPTS NaN and Infinity on the way back in, which is why this
    round-trips happily in a Python test and still breaks the browser.
    parse_constant is the hook for exactly those three literals, so raising
    there is what the browser's JSON.parse does.
    """
    def _reject(const):
        raise ValueError("invalid JSON literal: %s" % const)
    return json.loads(text, parse_constant=_reject)


try:
    _strict_loads(bad)
    strict_ok = True
except ValueError:
    strict_ok = False
check("  which a strict parser rejects", not strict_ok,
      "Python's own loads is lenient - which is why a Python round-trip "
      "test would have passed while the browser kept failing")
check("  a VALID frame still parses strictly",
      _strict_loads('{"v": 1.5}') == {"v": 1.5},
      "a guard that rejects everything proves nothing")
check("  the same is true of Infinity",
      "Infinity" in json.dumps({"v": float("inf")}))

print()
print("[the stream helper sends null instead]")
frame = {"type": "reading", "readings": [
    {"tag": "Flow", "value": float("nan")},
    {"tag": "Temp", "value": 37.0},
    {"tag": "Pressure", "value": float("inf")},
    {"tag": "Vacuum", "value": float("-inf")},
]}
out = _ws_json(frame)
check("no non-JSON literal survives",
      not re.search(r"(?<![\"\w])(NaN|-?Infinity)(?![\"\w])", out), out[:160])
back = _strict_loads(out)
check("  the frame round-trips through a STRICT parser", isinstance(back, dict),
      "this is the assertion that actually mirrors the browser")
vals = {r["tag"]: r["value"] for r in back["readings"]}
check("  a NaN reading becomes null", vals["Flow"] is None, vals)
check("  so does an infinite one",
      vals["Pressure"] is None and vals["Vacuum"] is None, vals)
check("  and a GOOD reading is untouched", vals["Temp"] == 37.0, vals)
check("the rest of the frame is unchanged", back["type"] == "reading")

print()
print("[one bad reading must not take the others with it]")
check("three good tags still arrive beside one faulted channel",
      len(back["readings"]) == 4 and vals["Temp"] == 37.0,
      "the defect: JSON.parse throws on the whole frame, so every tag on "
      "the gateway goes dark because of one")

print()
print("[nested and awkward shapes]")
deep = _ws_json({"a": {"b": [{"c": float("nan")}, 1.5]}, "t": "NaN is fine in a string"})
check("non-finite values are cleaned at any depth",
      json.loads(deep)["a"]["b"][0]["c"] is None, deep)
check("  a plain number survives", json.loads(deep)["a"]["b"][1] == 1.5)
check("  a string that merely says NaN is left alone",
      json.loads(deep)["t"] == "NaN is fine in a string",
      "a blanket text substitution would corrupt real data")
check("  an empty frame is still valid", _ws_json({}) == "{}")
check("  a list frame works too", json.loads(_ws_json([1, float("nan")])) == [1, None])
# json.dumps writes a float key through repr(), so a value-only cleaner
# leaves the literal key "NaN" behind - valid JSON with a corrupted name,
# which is worse than a loud failure because nothing reports it.
_keyed = _ws_json({float("nan"): 1, "Flow": 2})
check("  a non-finite dict KEY is cleaned as well as values",
      "NaN" not in _keyed and _strict_loads(_keyed)["Flow"] == 2, _keyed)
check("  booleans and None are not mangled",
      json.loads(_ws_json({"a": True, "b": None, "c": 0})) == {"a": True, "b": None, "c": 0},
      "bool is a subclass of int - a careless numeric check turns True into 1")

print()
print("[every frame built from device data goes through it]")
stream = MAIN[MAIN.index('@app.websocket("/ws/stream")'):]
stream = stream[:stream.index("async def _websocket_cloud_live_loop")]
check("the status frame uses the helper", "_ws_json(" in stream)
check("  and so does every queued event",
      "await websocket.send_text(_ws_json(event))" in stream,
      "this is the frame that carries readings")
check("  no raw json.dumps is left in that handler",
      "json.dumps" not in stream,
      "one unguarded send is enough to reproduce the fault")
check("the cloud-live frame is guarded too",
      "await websocket.send_text(_ws_json(payload))" in MAIN)

print()
print("[and the browser says what it could not parse]")
APP = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
              encoding="utf-8").read()
check("a parse failure logs the payload prefix",
      "Live stream payload did not parse:" in APP
      and 'String(event?.data ?? "").slice(0, 200)' in APP,
      "naming a fault while destroying the evidence of it is why this went "
      "undiagnosed: a bad frame and a JS bug looked identical")
check("  with the parser's own message",
      "did not parse: ${String(err?.message || err)}" in APP)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
