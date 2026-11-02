# -*- coding: utf-8 -*-
"""A side service must never be able to stall collection.

2026-10-30, found on the operator's machine with a live py-spy dump. The
gateway was configured for 1 s and delivering 8.02 s, with last_error=None:

    cycle_ms=8031 interval_ms=1000 tags=4
    read_ms=4   tel_ms=14   emit_ms=4   persist_ms=7998   persist_q=58

The PLC read was 4 ms. The 8 s was the persist step waiting on a timeout.
The dump named the culprit exactly:

    Thread 20652  is_running(opcua_server.py:43)
                  current_port(opcua_server.py:53)
                  opcua_status(connections.py:99)
    4x AnyIO      is_running(opcua_server.py:43)
    2x tn-persist publish_tag(opcua_server.py:293)

`current_port()` held the module lock and then called `is_running()`, which
takes the same lock. A threading.Lock is NOT reentrant, so that thread
deadlocked against itself WHILE HOLDING the lock, and everything behind it
waited for ever - the Connections page's own polling, and the gateway's
persist workers. The operator does not even license OPC UA.

Two independent guarantees are asserted here, because either alone would
have prevented the outage:

  1. the status helpers cannot deadlock, however they are called;
  2. collection does not enter the publish path when no service is running.

    python scripts/test_outbound_never_blocks_collection.py
"""
from __future__ import annotations

import os
import sys
import threading
import tempfile

_TMP = tempfile.mkdtemp(prefix="tn_outbound_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-62s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if detail else ""))


def within(seconds, fn):
    """Run fn in a thread; True if it finished in time. A deadlock hangs."""
    done = threading.Event()
    err = {}

    def run():
        try:
            fn()
        except Exception as exc:               # noqa: BLE001
            err["e"] = exc
        finally:
            done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return done.wait(seconds), err.get("e")


def main() -> int:
    print("outbound services must not stall collection")
    from app.services import opcua_server, mqtt_broker, connections_publish

    for name, mod in (("opcua", opcua_server), ("mqtt", mqtt_broker)):
        # THE bug: current_port() takes the lock then calls is_running(),
        # which takes it again. With a plain Lock this never returns.
        ok, exc = within(5.0, mod.current_port)
        check("%s: current_port() returns (no self-deadlock)" % name, ok and not exc,
              "timed out - the lock is not reentrant" if not ok else (str(exc) if exc else ""))

        ok, exc = within(5.0, mod.is_running)
        check("%s: is_running() returns" % name, ok and not exc)

        # And again from several threads at once, which is what the
        # Connections page does while the gateway publishes.
        def hammer():
            for _ in range(50):
                mod.current_port()
                mod.is_running()
        ok, exc = within(10.0, hammer)
        check("%s: concurrent status calls never wedge" % name, ok and not exc)

    # With nothing running, collection must not enter the publish path.
    ok, exc = within(5.0, connections_publish.any_outbound_active)
    check("any_outbound_active() answers quickly", ok and not exc)
    check("  and says NO when no service is running",
          connections_publish.any_outbound_active() is False)

    # Publishing with no server up must be a cheap no-op, not a block.
    def publish_many():
        for i in range(200):
            connections_publish.publish_opcua(
                gateway_id="gw", device_name="dev", tag_name="T%d" % i,
                value=1.0, ts_utc="2026-10-30 00:00:00", quality="GOOD")
    ok, exc = within(10.0, publish_many)
    check("200 publishes with no server up do not block", ok and not exc)

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
