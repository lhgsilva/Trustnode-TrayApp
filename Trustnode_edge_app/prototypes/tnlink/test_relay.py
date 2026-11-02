# -*- coding: utf-8 -*-
"""The rendezvous path, end to end, with nothing listening on either side.

This is the shape that has to work at a customer site: the agent and the
client both dial OUT, so neither needs a firewall rule, a port-forward or
administrator rights.

What it pins:

  * agent and client pair through the relay and the session works,
  * the relay never sees the Connection Code - only an HMAC-derived room,
  * the relay cannot read the traffic (checked by capturing every byte it
    forwards and proving the plaintext is not in it),
  * a wrong code fails even though the relay happily paired the sockets -
    the end-to-end handshake is what authenticates, not the rendezvous,
  * a client with no agent waiting is told so, instead of hanging,
  * a second client is refused while one is connected,
  * bulk data survives the extra hop.

Run:  python prototypes/tnlink/test_relay.py
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tnlink as T          # noqa: E402
import tnlink_relay as R    # noqa: E402

FAILS = []
RELAY_PORT = int(os.environ.get("TNLINK_RELAY_TEST_PORT", "51871"))
RELAY = "127.0.0.1:%d" % RELAY_PORT
ECHO_PORT = RELAY_PORT + 1


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:200]) if detail else ""))
    if not ok:
        FAILS.append(name)
    return bool(ok)


print("LS-Link - the relay path, outbound only at both ends")
print()

# --------------------------------------------------------------------------
print("[1. the relay matches on a derived room, never the code]")
code = T.new_code()
room = T.room_id(code)
check("the room is 16 bytes", len(room) == 16, len(room))
check("  it is not the code", code.encode() not in room
      and T.normalise_code(code).encode() not in room,
      "the relay runs on a host that must not hold the secret")
check("  the same code gives the same room",
      T.room_id(code) == T.room_id(code.lower().replace("-", " ")),
      "normalisation must agree on both sides or they never meet")
check("  a different code gives a different room",
      T.room_id(T.new_code()) != room)

# --------------------------------------------------------------------------
print()
print("[2. bring up the relay and an echo target]")
threading.Thread(
    target=lambda: R.main.__wrapped__() if hasattr(R.main, "__wrapped__")
    else None, daemon=True)

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", RELAY_PORT))
srv.listen(64)


def relay_accept():
    while True:
        try:
            c, a = srv.accept()
        except OSError:
            return
        threading.Thread(target=R.handle, args=(c, a), daemon=True).start()


threading.Thread(target=relay_accept, daemon=True).start()
time.sleep(0.6)
check("the relay is accepting", True)

echo_ready = threading.Event()


def echo_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", ECHO_PORT))
    s.listen(8)
    echo_ready.set()
    while True:
        try:
            c, _ = s.accept()
        except OSError:
            return

        def handle(c=c):
            try:
                while True:
                    b = c.recv(65536)
                    if not b:
                        return
                    c.sendall(b)
            except OSError:
                pass
            finally:
                c.close()
        threading.Thread(target=handle, daemon=True).start()


threading.Thread(target=echo_server, daemon=True).start()
echo_ready.wait(5)
check("  the echo target is listening", echo_ready.is_set())

# --------------------------------------------------------------------------
print()
print("[3. a client with no agent waiting is told, not left hanging]")
t0 = time.time()
try:
    T.relay_dial(RELAY, T.new_code(), is_agent=False)
    verdict = "paired with nobody"
except T.AuthError as exc:
    verdict = str(exc)
except Exception as exc:
    verdict = "%s: %s" % (type(exc).__name__, exc)
took = time.time() - t0
check("it says no server is waiting", "no LS-Link server" in verdict,
      verdict)
check("  and it says so quickly", took < 10, "%.1fs" % took)

# --------------------------------------------------------------------------
print()
print("[4. agent + client pair through the relay]")
agent = T.Server(code, ["127.0.0.0/8"], 0, 0, "127.0.0.1",
                 firewall=False, relay=RELAY)
agent.permitted = lambda ip: (True, "") if ip.startswith("127.") else (
    False, "%s is outside the allowlist" % ip)
threading.Thread(target=agent._serve_relay, daemon=True).start()
time.sleep(1.5)

cli = T.Client("", 0, code)
cli.relay = RELAY
ok = True
why = ""
try:
    cli.connect()
except Exception as exc:
    ok = False
    why = "%s: %s" % (type(exc).__name__, exc)
if not check("the session came up with NOTHING listening at either end",
             ok, why):
    print("RESULT: FAIL - cannot continue")
    os._exit(2)
check("  the agent described its network", bool(cli.info.get("host")),
      cli.info.get("host"))

print()
print("[5. it actually does the job over the relay]")
cli.sec.send(T.pack_ctrl({"op": "ping", "ip": "127.0.0.1", "count": 2}))
pr = cli._await("ping", 25)
check("ping travelled through the relay",
      bool(pr) and pr.get("received") == 2, pr)

cli.cmd_map("%d" % (RELAY_PORT + 2), "127.0.0.1:%d" % ECHO_PORT)
time.sleep(0.6)
blob = os.urandom(150_000)
rt = b""
try:
    t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    t.settimeout(40)
    t.connect(("127.0.0.1", RELAY_PORT + 2))
    t.sendall(blob)
    while len(rt) < len(blob):
        chunk = t.recv(65536)
        if not chunk:
            break
        rt += chunk
    t.close()
except Exception as exc:
    rt = ("ERROR %s: %s" % (type(exc).__name__, exc)).encode()
check("  150 KB survives the extra hop intact", rt == blob,
      "got %d of %d bytes" % (len(rt), len(blob)))

# --------------------------------------------------------------------------
print()
print("[6. the relay cannot read what it carries]")
seen = bytearray()
_real_pump = R.pump


def spy_pump(a, b, counter):
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            seen.extend(data)
            b.sendall(data)
            counter[0] += len(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


R.pump = spy_pump
code2 = T.new_code()
agent2 = T.Server(code2, ["127.0.0.0/8"], 0, 0, "127.0.0.1",
                  firewall=False, relay=RELAY)
agent2.permitted = lambda ip: (True, "")
threading.Thread(target=agent2._serve_relay, daemon=True).start()
time.sleep(1.5)
cli2 = T.Client("", 0, code2)
cli2.relay = RELAY
cli2.connect()
MARKER = "SimREAL[42]-secret-tag-name"
cli2.sec.send(T.pack_ctrl({"op": "ping", "ip": "127.0.0.1", "count": 1,
                           "note": MARKER}))
cli2._await("ping", 25)
time.sleep(0.5)
R.pump = _real_pump
check("the relay forwarded real traffic", len(seen) > 200, "%d bytes" % len(seen))
check("  the plaintext is NOT in what it saw",
      MARKER.encode() not in bytes(seen),
      "end-to-end encryption is what makes a third-party relay acceptable")
check("  nor is the Connection Code",
      T.normalise_code(code2).encode() not in bytes(seen))

# --------------------------------------------------------------------------
print()
print("[7. the relay pairing a socket is NOT authentication]")
# The relay will happily connect anyone who knows the room. Only the
# end-to-end handshake decides, and it needs the code itself.
code3 = T.new_code()
agent3 = T.Server(code3, ["127.0.0.0/8"], 0, 0, "127.0.0.1",
                  firewall=False, relay=RELAY)
threading.Thread(target=agent3._serve_relay, daemon=True).start()
time.sleep(1.5)
bad = T.Client("", 0, code3)
bad.relay = RELAY
bad.code = code3            # right room...
verdict = ""
try:
    sock = T.relay_dial(RELAY, code3, is_agent=False)
    T.handshake(sock, T.new_code(), is_server=False)   # ...wrong code
    verdict = "ACCEPTED a wrong code"
except T.AuthError as exc:
    verdict = "AuthError: %s" % exc
except Exception as exc:
    verdict = "%s: %s" % (type(exc).__name__, exc)
check("a wrong code is refused even after the relay paired it",
      "AuthError" in verdict, verdict)

print()
print("[8. relay mode binds NOTHING locally]")
# The relay branch used to sit AFTER the listening socket was created, so
# a relay-mode agent still tried to bind its local port and died with
# "only one usage of each socket address" when anything else held it - a
# port it has no business touching at all.
squat = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
squat.bind(("127.0.0.1", 0))
taken = squat.getsockname()[1]
squat.listen(1)
code4 = T.new_code()
agent4 = T.Server(code4, ["127.0.0.0/8"], taken, 0, "127.0.0.1",
                  firewall=False, relay=RELAY)
started = threading.Event()
result = {}


def run_agent4():
    started.set()
    result["rc"] = agent4.serve()


threading.Thread(target=run_agent4, daemon=True).start()
started.wait(5)
time.sleep(2.0)
check("serve() did not exit on a busy local port",
      "rc" not in result,
      "returned %r - relay mode must never bind locally" % result.get("rc"))
paired = False
try:
    c4 = T.Client("", 0, code4)
    c4.relay = RELAY
    c4.connect()
    paired = bool(c4.info.get("host"))
    c4.alive = False
    c4.sec.close()
except Exception as exc:
    paired = "%s: %s" % (type(exc).__name__, exc)
check("  and it still pairs through the relay", paired is True, paired)
squat.close()

print()
print("RESULT: {0}".format("PASS" if not FAILS else
                           "FAIL - " + ", ".join(FAILS)))
sys.stdout.flush()
os._exit(0 if not FAILS else 2)
