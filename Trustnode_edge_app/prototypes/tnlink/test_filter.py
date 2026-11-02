# -*- coding: utf-8 -*-
"""The flood filter must never eat ARP.

An ARP REPLY is UNICAST - addressed to whoever asked. The first version
tested "is this unicast for a MAC I know?" before it tested "is this
ARP?", so replies from the PLC were dropped as "unicast for someone
else". The remote PC then printed

    Reply from 192.168.10.77: Destination host unreachable.

which is Windows answering its own ARP request with a shrug. Nothing
works without ARP, so it is checked first and never filtered.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tnlink_bridge as B  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print("  %-52s: %s%s" % (name, "PASS" if ok else "FAIL",
                             (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


PLC_MAC = bytes.fromhex("5c8816ccf50d")
PC_MAC = bytes.fromhex("02544e4c0001")
OTHER = bytes.fromhex("aabbccddeeff")


def eth(dst, src, et, body=b"\x00" * 46):
    return dst + src + et.to_bytes(2, "big") + body


def ipv4(dst4, proto=17, sport=0, dport=0):
    h = (bytes([0x45, 0, 0, 40, 0, 0, 0x40, 0, 64, proto, 0, 0])
         + bytes([192, 168, 10, 55]) + bytes(dst4))
    return h + sport.to_bytes(2, "big") + dport.to_bytes(2, "big") + b"\x00" * 4


print("LS-Link - the flood filter keeps what matters")
print()

f = B.FrameFilter()
# The far side introduces itself, as it does in a real session.
f.learn(eth(PLC_MAC, PC_MAC, 0x0806, b"\x00" * 28))

print("[1. ARP survives, in both directions and both shapes]")
check("an ARP request (broadcast) is kept",
      f.allow(eth(b"\xff" * 6, PLC_MAC, 0x0806, b"\x00" * 28)))
check("  an ARP REPLY (unicast) is kept",
      f.allow(eth(PC_MAC, PLC_MAC, 0x0806, b"\x00" * 28)),
      "this is the one that was being dropped")
check("  an ARP reply to an UNKNOWN mac is still kept",
      f.allow(eth(OTHER, PLC_MAC, 0x0806, b"\x00" * 28)),
      "the remote PC's adapter gets a new MAC every session")

print()
print("[2. ordinary traffic survives]")
check("unicast IPv4 to the remote PC is kept",
      f.allow(eth(PC_MAC, PLC_MAC, 0x0800, ipv4([192, 168, 10, 55], 6))))
check("  unicast to a MAC we have not seen is kept",
      f.allow(eth(OTHER, PLC_MAC, 0x0800, ipv4([192, 168, 10, 99], 6))),
      "a learning bridge stops flooding on its own")
check("  DHCP is kept", f.allow(
    eth(b"\xff" * 6, PLC_MAC, 0x0800, ipv4([255, 255, 255, 255], 17, 68, 67))),
      "it is how the remote PC gets an address at all")

print()
print("[3. and the flood is still dropped]")
check("EtherNet/IP I/O multicast is dropped", not f.allow(
    eth(bytes.fromhex("01005e400101"), PLC_MAC, 0x0800,
        ipv4([239, 192, 1, 1], 17, 2222, 2222))),
      "a Logix system streams this continuously")
check("  mDNS is dropped", not f.allow(
    eth(bytes.fromhex("01005e0000fb"), PLC_MAC, 0x0800,
        ipv4([224, 0, 0, 251], 17, 5353, 5353))))
check("  SSDP is dropped", not f.allow(
    eth(bytes.fromhex("01005e7ffffa"), PLC_MAC, 0x0800,
        ipv4([239, 255, 255, 250], 17, 1900, 1900))))
check("  IPv6 is dropped",
      not f.allow(eth(bytes.fromhex("3333ff000001"), PLC_MAC, 0x86DD)))
check("  spanning tree is dropped",
      not f.allow(eth(bytes.fromhex("0180c2000000"), PLC_MAC, 0x0026)))

print()
print("  %s" % f.summary())
print()
print("RESULT: %s" % ("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
