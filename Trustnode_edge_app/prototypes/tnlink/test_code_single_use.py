# -*- coding: utf-8 -*-
"""A connection code reaches ONE machine, ONCE.

2026-10-22, reported from a site: an engineer paired, worked for forty
minutes, and only then found they were on YESTERDAY'S computer. A server
left running there still owned that code's room on the relay and re-armed
itself after every session, so the code kept working for ever and nothing
on screen said which machine had answered.

Three properties are checked here, all offline:

  * codes are unique - two servers never mint the same one;
  * a code is SINGLE USE - once a session ends the server moves to a new
    code, so the old one can never reach that machine again;
  * the room the relay matches on changes with it, which is what actually
    strands the dead code.

Plus the crash that ended that session: the server tears a share down with
tunnel.down(), which is the CLIENT's Endpoint method name. The server's
NetStack only had stop(), so ending a share raised AttributeError and the
link was reported as DROPPED instead of closed.
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAILS = []


def check(name, ok, detail=""):
    print("  %-56s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  " + str(detail)[:110]) if not ok else ""))
    if not ok:
        FAILS.append(name)


print("A connection code reaches one machine, once")
print()

import tnlink  # noqa: E402

# ---- unique -------------------------------------------------------------
codes = {tnlink.new_code() for _ in range(2000)}
check("2000 codes, no repeat", len(codes) == 2000, "%d unique" % len(codes))
check("  each is long enough to be unguessable",
      all(len(c.replace("-", "")) >= 15 for c in codes))

rooms = {tnlink.room_id(c) for c in list(codes)[:500]}
check("  a different code means a different relay room", len(rooms) == 500,
      "%d rooms for 500 codes" % len(rooms))
check("  the same code always maps to the same room",
      tnlink.room_id("TN-ABCDE-FGHJK-MNPQR") == tnlink.room_id("tn abcde fghjk mnpqr"),
      "the relay must still match a pasted code")
check("  the room is NOT the code", tnlink.room_id("TN-ABCDE-FGHJK-MNPQR")
      != b"TN-ABCDE-FGHJK-MNPQR"[:16],
      "the relay is someone else's machine; it must not hold the secret")

# ---- single use ---------------------------------------------------------
src = open(os.path.join(HERE, "tnlink.py"), encoding="utf-8").read()
tail = src[src.index("log(\"PAIRED\")"):]
tail = tail[:tail.index("# -- lifecycle")]
check("the server mints a NEW code when a session ends",
      re.search(r"self\.code\s*=\s*new_code\(\)", tail) is not None,
      "without this the same code serves for ever - which is how a "
      "forgotten server on an old PC kept answering")
check("  and says the old one is dead",
      "is now dead" in tail, "the operator must know not to re-send it")

# ---- the teardown crash -------------------------------------------------
import tnlink_netstack as NS  # noqa: E402

stack = NS.NetStack(lambda b: None, [], log=lambda m: None)
check("NetStack answers to down(), like the client's Endpoint",
      hasattr(stack, "down"),
      "the server calls tunnel.down(); AttributeError was reported as "
      "'LINK DROPPED'")
if hasattr(stack, "down"):
    stack.down()
    check("  and down() really stops it", stack.alive is False, stack.alive)

# ---- the client says WHICH machine ---------------------------------------
check("the client announces the peer on connect",
      "_announce_peer" in src and "CONNECTED TO" in src,
      "a code says nothing about which machine it reaches")

print()
print("RESULT: %s" % ("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
