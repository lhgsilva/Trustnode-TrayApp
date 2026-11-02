# -*- coding: utf-8 -*-
"""The newest pairing wins, the code dies at pairing, and both ends know who.

2026-11-02, reported: "the saved or recent connections still not working - we
should be able to reconnect to the same computer", "when a new connection is
established it should drop any existing connection", and "every time a new
connection is started the app throws a message that the service restarted".

What was in the way, measured in the code:

  * Server.serve() handled ONE client at a time - after a handshake it sat
    in _session() and never called accept() again until that session ended.
    An engineer whose previous process died (the plant had not noticed) was
    queued behind their own dead session until the handshake timed out; over
    the relay the room answered "busy".
  * The code rotated only when the session ENDED, so during a live session
    the plant still expected the OLD code while the saved connection
    presented the NEXT one it had been handed.

Now sessions run in their own threads, the accept loop keeps accepting, a
successful pairing replaces whatever session is there, and the code is spent
the moment it pairs - the only code that opens a new session is the one
handed over the encrypted channel to the engineer who is connected. So "the
same client again" is precise, and anyone who merely overheard the first
code is refused.

    python prototypes/tnlink/test_newest_wins.py
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="tnlink_nw_")
os.environ["APPDATA"] = _TMP

import tnlink as T                                              # noqa: E402

PASS, FAIL = [], []
PORT = 51977


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-66s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def _raw_pair(code, timeout=6.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(("127.0.0.1", PORT))
    return T.handshake(s, code, is_server=False)


def main() -> int:
    print("LS-Link: the newest pairing wins and the code dies at pairing")
    code0 = T.new_code()
    srv = T.Server(code0, ["127.0.0.0/8"], PORT, 0, "127.0.0.1", firewall=False)
    threading.Thread(target=srv.serve, daemon=True).start()
    time.sleep(0.8)

    # ---- the first engineer pairs and is handed the next code ------------
    a = T.Client("127.0.0.1", PORT, code0)
    a.connect()
    n1 = str(a.info.get("next_code") or "")
    check("engineer A pairs with the plant's code", bool(a.sec) and bool(a.info.get("host")))
    check("  and is handed the next code over the encrypted channel", bool(n1) and n1 != code0)
    time.sleep(0.3)
    sec_a = srv.sec
    check("the plant's code is spent the moment A paired", srv.code == n1 and srv.code != code0,
          "server code=%s" % srv.code)
    check("the plant knows WHO is connected",
          (srv.peer or {}).get("unit") == T.unit_id() and bool((srv.peer or {}).get("host")),
          repr(srv.peer))

    # ---- a stranger with the first code is refused while A is connected --
    refused = ""
    try:
        _raw_pair(code0)
        refused = "PAIRED - the spent code was accepted"
    except T.AuthError as exc:
        refused = "AuthError: %s" % exc
    except Exception as exc:                                    # noqa: BLE001
        refused = "%s: %s" % (type(exc).__name__, exc)
    check("a stranger with the FIRST code is refused during A's session",
          refused.startswith("AuthError"), refused)
    check("  and A is still connected", sec_a is not None and srv.sec is sec_a)

    # ---- A's process dies without the plant noticing: the socket stays ---
    # open and silent. The same engineer comes back with the next code.
    b = T.Client("127.0.0.1", PORT, n1)
    t0 = time.perf_counter()
    b.connect()
    took = time.perf_counter() - t0
    n2 = str(b.info.get("next_code") or "")
    check("the same engineer reconnects with the stored code while A's session is open",
          bool(b.sec) and took < 5.0, "%.1fs" % took)
    time.sleep(0.5)
    check("  the plant adopted the NEW session", srv.sec is not None and srv.sec is not sec_a)
    dead = ""
    try:
        a.sec.sock.settimeout(3.0)
        a.sec.recv()
        dead = "A's socket still delivers frames"
    except Exception as exc:                                    # noqa: BLE001
        dead = "%s" % type(exc).__name__
    check("  and dropped the old one", "still delivers" not in dead, dead)
    check("  B holds a fresh next code", bool(n2) and n2 not in (code0, n1))
    check("  the plant's code moved on again", srv.code == n2, srv.code)

    # ---- the code B presented is spent too ------------------------------
    try:
        _raw_pair(n1)
        refused = "PAIRED"
    except T.AuthError as exc:
        refused = "AuthError: %s" % exc
    except Exception as exc:                                    # noqa: BLE001
        refused = "%s" % type(exc).__name__
    check("the code B used cannot open a third session", refused.startswith("AuthError"), refused)

    # ---- B leaves cleanly; the next code still opens the plant -----------
    b.alive = False
    b.sec.close()
    time.sleep(0.8)
    check("after B leaves, nobody is connected", srv.sec is None and not srv.peer, repr(srv.peer))
    c = T.Client("127.0.0.1", PORT, n2)
    c.connect()
    check("a reconnect after a clean disconnect works with the stored code", bool(c.sec))
    c.alive = False
    c.sec.close()

    # ---- the tray never says "restarted" for a connection ----------------
    with open(os.path.join(HERE, "tnlink_service.py"), encoding="utf-8") as fh:
        svc = fh.read()
    check("connecting does not announce a service restart",
          "def _reconnect(" in svc and 'self._reconnect(label or "the plant")' in svc
          and svc.count('"Service restarted."') == 1)
    check("  a refused stored code is explained and cleared",
          "clear_next_code" in svc and "refused the stored code" in svc)
    check("  the plant's screen gets the engineer's name", '"peer_unit": peer_unit' in svc)
    with open(os.path.join(HERE, "ui", "index.html"), encoding="utf-8") as fh:
        ui = fh.read()
    check("the card shows the connected computer on both ends",
          'id="peer-title"' in ui and "Connected engineer" in ui and 'id="peer-unit"' in ui)
    check("  and the saved connection that is live is marked", "connectedUnit" in ui and "Reconnect" in ui)

    srv.stop()
    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
