# -*- coding: utf-8 -*-
"""Ask an ifm block, over its FIELDBUS port, what it will actually give us.

Copy this ONE file to the computer that talks to the block and run:

    python diagnose_ifm_fieldbus.py 192.168.1.250
    python diagnose_ifm_fieldbus.py 192.168.1.250 --json before.json

Nothing else is needed - no TrustNode install, no repository, no pip install.
It speaks EtherNet/IP directly using only the Python standard library, because
a diagnostic that only runs on a developer's machine is no use on the day a
block will not read. (The first version imported TrustNode's driver, which in
turn wanted pycomm3; on a plant laptop that is two things that are not there.)

diagnose_ifm.py covers the IoT Core side (HTTP/JSON, port 80). This is the
other half: EtherNet/IP on 44818, which is what a block wired to its FIELDBUS
port serves - and there it serves no IoT Core at all, which is why the block
finder cannot see it.

WHY THIS EXISTS

An ifm master's input assembly is 446 bytes on an 8-port model. TrustNode maps
exactly two of them: byte 0 is pin 4 and byte 1 is pin 2, one bit per port.
That is the digital-input image and nothing else - IO-Link process data and
port status live further into those 446 bytes, and TrustNode does not currently
know where.

Nobody should guess those offsets. An EDS declares the assembly's instance and
size and stops there; it never says what a bit means. Guessing produced
"0.000 V from a healthy-looking meter" on the EM122 in August, and it would
produce plausible wrong numbers here too.

So this dumps the WHOLE input assembly as raw bytes and decodes the part we do
understand. Run it, change something physical, run it again: the bytes that
differ are that port's data.

WHAT TO SEND BACK

Both JSON files and the console output. Read-only throughout: identity and
assembly READS. Nothing is written to the block.
"""
from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time

CIP_PORT = 44818

# Assembly instances an ifm master is documented to publish, plus neighbours
# worth trying so an unfamiliar model still reports something.
CANDIDATES = (100, 101, 102, 103, 104, 105, 110, 120, 150, 151, 152, 198, 199)

ASSEMBLY_CLASS = 0x04
IDENTITY_CLASS = 0x01
GET_ATTRIBUTE_SINGLE = 0x0E
GET_ATTRIBUTES_ALL = 0x01
ASSEMBLY_DATA_ATTRIBUTE = 3

# The CIP general status codes worth naming. Anything else is printed raw.
CIP_STATUS = {
    0x00: "success",
    0x04: "path segment error",
    0x05: "path destination unknown - no such class/instance on this device",
    0x08: "service not supported",
    0x0A: "attribute list error",
    0x13: "not enough data",
    0x14: "attribute not supported",
    0x15: "too much data",
    0x16: "object does not exist",
    0x1E: "embedded service error",
}


def _say(line=""):
    print(line, flush=True)


def _rule(title):
    _say()
    _say("=" * 70)
    _say(title)
    _say("=" * 70)


class CipError(Exception):
    pass


class Cip:
    """Just enough EtherNet/IP to identify a device and read an assembly."""

    def __init__(self, host, timeout=5.0):
        self.host = host
        self.timeout = float(timeout)
        self.sock = None
        self.session = 0

    def __enter__(self):
        self.sock = socket.create_connection((self.host, CIP_PORT), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        self._register()
        return self

    def __exit__(self, *exc):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass

    # -- encapsulation ----------------------------------------------------
    def _encap(self, command, data=b""):
        return struct.pack("<HHII8sI", command, len(data), self.session,
                           0, b"\x00" * 8, 0) + data

    def _exchange(self, command, data=b""):
        self.sock.sendall(self._encap(command, data))
        head = self._recv_exact(24)
        _cmd, length, session, status = struct.unpack("<HHII", head[:12])
        body = self._recv_exact(length) if length else b""
        if status != 0:
            raise CipError("encapsulation status 0x%08x" % status)
        return session, body

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise CipError("connection closed by the device")
            buf += chunk
        return buf

    def _register(self):
        session, _ = self._exchange(0x0065, struct.pack("<HH", 1, 0))
        self.session = session
        if not session:
            raise CipError("device refused a session")

    # -- CIP --------------------------------------------------------------
    @staticmethod
    def _path(class_id, instance, attribute=None):
        out = b"\x20" + bytes([class_id]) if class_id <= 0xFF else \
            b"\x21\x00" + struct.pack("<H", class_id)
        if instance <= 0xFF:
            out += b"\x24" + bytes([instance])
        else:
            out += b"\x25\x00" + struct.pack("<H", instance)
        if attribute is not None:
            out += b"\x30" + bytes([attribute])
        return out

    def request(self, service, class_id, instance, attribute=None):
        path = self._path(class_id, instance, attribute)
        if len(path) % 2:
            path += b"\x00"
        mr = bytes([service, len(path) // 2]) + path
        cpf = (struct.pack("<H", 2)
               + struct.pack("<HH", 0x0000, 0)          # null address item
               + struct.pack("<HH", 0x00B2, len(mr)) + mr)
        _s, body = self._exchange(0x006F, struct.pack("<IH", 0, 10) + cpf)
        # interface handle (4) + timeout (2), then CPF
        cpf_body = body[6:]
        count = struct.unpack("<H", cpf_body[:2])[0]
        off = 2
        payload = b""
        for _ in range(count):
            item_type, item_len = struct.unpack("<HH", cpf_body[off:off + 4])
            off += 4
            item = cpf_body[off:off + item_len]
            off += item_len
            if item_type == 0x00B2:
                payload = item
        if len(payload) < 4:
            raise CipError("no CIP reply in the response")
        _svc, _res, status, addl = payload[0], payload[1], payload[2], payload[3]
        data = payload[4 + addl * 2:]
        if status != 0:
            raise CipError("CIP status 0x%02x (%s)"
                           % (status, CIP_STATUS.get(status, "unknown")))
        return data

    def identity(self):
        data = self.request(GET_ATTRIBUTES_ALL, IDENTITY_CLASS, 1)
        if len(data) < 15:
            raise CipError("identity reply too short")
        vendor_id, dev_type, prod_code, rev_major, rev_minor, status, serial = \
            struct.unpack("<HHHBBHI", data[:14])
        name_len = data[14]
        name = data[15:15 + name_len].decode("ascii", "replace")
        return {
            "vendor_id": vendor_id,
            "vendor": "ifm electronic" if vendor_id == 322 else
                      ("Rockwell Automation/Allen-Bradley" if vendor_id == 1
                       else "vendor id %d" % vendor_id),
            "device_type": dev_type,
            "product_code": prod_code,
            "revision": "%d.%d" % (rev_major, rev_minor),
            "status": "0x%04x" % status,
            "serial": "%08x" % serial,
            "product_name": name,
        }

    def read_assembly(self, instance):
        return self.request(GET_ATTRIBUTE_SINGLE, ASSEMBLY_CLASS, int(instance),
                            ASSEMBLY_DATA_ATTRIBUTE)


def _hexdump(data, width=16, limit=512):
    shown = data[:limit]
    for off in range(0, len(shown), width):
        chunk = shown[off:off + width]
        _say("   %04d  %s" % (off, " ".join("%02x" % b for b in chunk)))
    if len(data) > limit:
        _say("   ... %d more bytes" % (len(data) - limit))


def ifm_pin_bits(raw, port_count):
    """The map TrustNode uses: byte 0 = pin 4, byte 1 = pin 2, bit N-1 = port N."""
    out = []
    for port in range(1, int(port_count) + 1):
        bit = port - 1
        for pin, base in (("Pin4", 0), ("Pin2", 1)):
            idx = base + (bit // 8) * 2
            val = None
            if idx < len(raw):
                val = bool(raw[idx] & (1 << (bit % 8)))
            out.append(("Port%d_%s" % (port, pin), val))
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Diagnose an ifm block over EtherNet/IP (fieldbus port).")
    ap.add_argument("host", help="block IP, e.g. 192.168.1.250")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--ports", type=int, default=8,
                    help="how many IO-Link ports the block has (4 or 8)")
    ap.add_argument("--json", default="", help="also write the raw findings here")
    args = ap.parse_args()

    report = {"host": args.host, "when": time.strftime("%Y-%m-%d %H:%M:%S")}
    _say("TrustNode - ifm block diagnosis, FIELDBUS side")
    _say("block: %s   port: %d" % (args.host, CIP_PORT))

    _rule("1. Does anything answer EtherNet/IP on 44818?")
    if not _tcp_open(args.host, CIP_PORT, args.timeout):
        _say("   port %d is CLOSED or filtered." % CIP_PORT)
        _say()
        _say("   Worth checking, roughly in order:")
        _say("     - the cable is in the block's FIELDBUS socket, not the IoT one")
        _say("     - ping %s from this machine" % args.host)
        _say("     - this PC is on the same subnet (ipconfig)")
        _say("     - a VPN will silently swallow LAN traffic - disconnect it")
        _write(args.json, report)
        return 2
    _say("   port %d is OPEN" % CIP_PORT)

    info = {}
    try:
        with Cip(args.host, args.timeout) as c:
            t0 = time.time()
            info = c.identity()
            _say("   identity answered in %.0f ms" % ((time.time() - t0) * 1000))
            for k in ("vendor", "vendor_id", "product_name", "product_code",
                      "revision", "serial", "status"):
                _say("   %-14s %s" % (k, info.get(k)))
    except Exception as exc:
        _say("   identity FAILED: %s: %s" % (type(exc).__name__, exc))
        report["identity_error"] = str(exc)
    report["identity"] = info

    if info and info.get("vendor_id") not in (322, None):
        _say()
        _say("   NOTE: this is not an ifm device (vendor id %s)."
             % info.get("vendor_id"))
        _say("   The pin map below is the ifm layout and will NOT match it.")

    _rule("2. Which assemblies can be read, and how big are they?")
    found = []
    for inst in CANDIDATES:
        try:
            with Cip(args.host, args.timeout) as c:
                data = c.read_assembly(inst)
            if data:
                found.append({"instance": inst, "size": len(data), "hex": data.hex()})
                _say("   %3d : %4d bytes  READABLE" % (inst, len(data)))
        except Exception as exc:
            _say("   %3d : %s" % (inst, str(exc)[:74]))
    report["assemblies"] = [{"instance": f["instance"], "size": f["size"]}
                            for f in found]
    if not found:
        _say()
        _say("   Nothing readable. Send this output on - the instance numbers")
        _say("   differ between families and we will work out yours.")
        _write(args.json, report)
        return 2

    chosen = next((f for f in found if f["instance"] == 100), None) or max(
        found, key=lambda f: f["size"])
    raw = bytes.fromhex(chosen["hex"])
    _say()
    _say("   using assembly %d (%d bytes) as the input image"
         % (chosen["instance"], chosen["size"]))
    guessed = {446: 8, 246: 4}.get(chosen["size"])
    if guessed:
        _say("   that size means a %d-port master" % guessed)

    _rule("3. The whole input image, as bytes")
    _say("   THIS IS THE IMPORTANT PART. TrustNode currently maps only bytes 0")
    _say("   and 1 of this image. Everything else - IO-Link process data, port")
    _say("   status, diagnostics - is in here, unmapped.")
    _say()
    _hexdump(raw)
    report["input_assembly"] = {"instance": chosen["instance"],
                                "size": chosen["size"], "hex": chosen["hex"]}

    _rule("4. The digital inputs, decoded the way TrustNode does it")
    _say("   byte 0 = 0x%02x   byte 1 = 0x%02x"
         % (raw[0] if raw else 0, raw[1] if len(raw) > 1 else 0))
    _say()
    decoded = []
    for name, val in ifm_pin_bits(raw, guessed or args.ports):
        decoded.append({"name": name, "value": val})
        _say("   %-16s %s" % (name, val))
    report["digital_inputs"] = decoded

    _rule("5. Now do it again with something changed")
    _say("   Cover a sensor, toggle an input, or unplug a port, then run:")
    _say("     python diagnose_ifm_fieldbus.py %s --json after.json" % args.host)
    _say()
    _say("   The bytes that differ between the two dumps are that port's data.")
    _say("   That is what makes the IO-Link mapping a fact instead of a guess.")

    _rule("6. What this means for the gateway")
    _say("   A block on its fieldbus socket must be configured as:")
    _say("     gateway type : EtherNet/IP device  - NOT 'IFM IO-Link master'")
    _say("     port         : 44818               - NOT 80")
    _say("     assembly     : %d" % chosen["instance"])
    _say()
    _say("   'IFM IO-Link master' is the IoT-Core path (HTTP, port 80). A block")
    _say("   wired to fieldbus serves no IoT Core, so that combination reads")
    _say("   nothing at all.")

    _write(args.json, report)
    return 0


def _tcp_open(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=min(3.0, timeout)):
            return True
    except Exception:
        return False


def _write(path, report):
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        _say()
        _say("   raw findings written to %s" % path)
    except Exception as exc:
        _say("   could not write %s: %s" % (path, exc))


if __name__ == "__main__":
    sys.exit(main())
