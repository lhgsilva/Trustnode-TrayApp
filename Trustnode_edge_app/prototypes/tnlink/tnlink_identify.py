# -*- coding: utf-8 -*-
"""Ask a device what it is, instead of guessing from a port number.

2026-10-29, requested: "the list of the devices, I would like the code try
to retrieve information about supplier and part number, give us an idea
what type of device or module was found".

A port tells you the protocol, not the product: 44818 is every
Allen-Bradley controller, every ifm block and every drive on the network.
Two sources answer properly, and both are read-only:

  * EtherNet/IP ListIdentity (UDP and TCP 44818, encapsulation command
    0x63). Devices answer it unsolicited and without a session - it is
    what RSWho uses to populate a browse list. It carries the vendor ID,
    device type, product code, revision, serial number and the product
    NAME as the maker wrote it.
  * An HTTP server header on 80/443, for the panels and drives whose only
    identity is their web page.

Nothing here writes to a device, opens a CIP session or touches a
controller's mode. The worst case is a timeout.
"""
from __future__ import annotations

import socket
import struct

#: ODVA vendor IDs. Only the ones a plant floor actually contains - a
#: complete list is thousands of rows and would be stale by shipping.
#: An unknown ID is reported as its number rather than guessed at.
VENDORS = {
    1: "Allen-Bradley", 5: "Schneider", 26: "Festo", 40: "WAGO",
    47: "SMC", 48: "Phoenix Contact", 49: "SICK", 53: "Pepperl+Fuchs",
    57: "Beckhoff", 108: "ABB", 115: "Omron", 127: "Lenze",
    133: "Balluff", 161: "Siemens", 256: "Turck", 283: "ifm electronic",
    291: "Banner", 356: "FANUC", 448: "Yaskawa", 500: "Bosch Rexroth",
    678: "Keyence", 806: "Murr Elektronik", 900: "Hilscher",
    1008: "Emerson", 1105: "Mitsubishi",
}

#: CIP device types (Identity object attribute 2).
DEVICE_TYPES = {
    0x00: "generic", 0x02: "AC drive", 0x03: "motor overload",
    0x04: "limit switch", 0x05: "inductive sensor", 0x06: "photo sensor",
    0x07: "general purpose discrete I/O", 0x09: "resolver",
    0x0C: "communications adapter", 0x0E: "programmable logic controller",
    0x10: "position controller", 0x13: "DC drive", 0x15: "contactor",
    0x16: "motor starter", 0x17: "soft start", 0x18: "human machine interface",
    0x1A: "mass flow controller", 0x1B: "pneumatic valve",
    0x1C: "vacuum pressure gauge", 0x1D: "process control valve",
    0x1E: "residual gas analyser", 0x1F: "DC power generator",
    0x20: "RF power generator", 0x21: "turbomolecular vacuum pump",
    0x22: "encoder", 0x23: "safety discrete I/O",
    0x24: "fluid flow controller", 0x25: "CIP motion drive",
    0x26: "CompoNet repeater", 0x27: "mass flow controller",
    0x28: "CIP modbus device", 0x29: "CIP modbus translator",
    0x2A: "safety analog I/O", 0x2B: "generic device (keyable)",
    0x2C: "managed switch", 0x32: "ENet/IP to DeviceNet",
}

_LIST_IDENTITY = struct.pack("<HHIIQI", 0x63, 0, 0, 0, 0, 0)


def _parse_identity(data: bytes) -> dict:
    """Pull the Identity item out of a ListIdentity reply.

    Layout after the 24-byte encapsulation header: item count (2), item
    type (2), item length (2), encapsulation protocol version (2), then
    the 16-byte sockaddr, then vendor, device type, product code,
    revision (2 bytes), status, serial, and a counted product name.
    """
    if len(data) < 26 or data[0] != 0x63:
        return {}
    try:
        body = data[24:]
        (count,) = struct.unpack_from("<H", body, 0)
        if count < 1:
            return {}
        off = 2 + 2 + 2 + 2 + 16          # item hdr + version + sockaddr
        vendor, dtype, pcode, rev_major, rev_minor = struct.unpack_from(
            "<HHHBB", body, off)
        off += 8
        off += 2 + 4                      # status + serial number
        name_len = body[off]
        off += 1
        name = body[off:off + name_len].decode("latin-1", "replace").strip()
        return {
            "vendor_id": vendor,
            "vendor": VENDORS.get(vendor, "vendor %d" % vendor),
            "type": DEVICE_TYPES.get(dtype, "type 0x%02X" % dtype),
            "product": name,
            "part": "%d" % pcode,
            "revision": "%d.%d" % (rev_major, rev_minor),
        }
    except Exception:                                     # noqa: BLE001
        return {}


def enet_ip(open_tcp, ip: str, timeout: float = 6.0) -> dict:
    """ListIdentity over TCP 44818, through whatever carries the session.

    `open_tcp(ip, port, timeout)` returns a connected socket-like object,
    so this works the same on the plant PC (a direct socket) and from the
    engineer (through the tunnel).
    """
    try:
        s = open_tcp(ip, 44818, timeout)
    except Exception:                                     # noqa: BLE001
        return {}
    try:
        s.settimeout(timeout)
        s.sendall(_LIST_IDENTITY)
        data = s.recv(1024)
        return _parse_identity(data)
    except Exception:                                     # noqa: BLE001
        return {}
    finally:
        try:
            s.close()
        except Exception:
            pass


def http_banner(open_tcp, ip: str, port: int = 80, timeout: float = 5.0) -> dict:
    """Whatever a web-only device says about itself in its headers."""
    try:
        s = open_tcp(ip, port, timeout)
    except Exception:                                     # noqa: BLE001
        return {}
    try:
        s.settimeout(timeout)
        s.sendall(b"HEAD / HTTP/1.0\r\nHost: %s\r\n\r\n" % ip.encode())
        raw = s.recv(2048).decode("latin-1", "replace")
        out = {}
        for line in raw.splitlines():
            low = line.lower()
            if low.startswith("server:"):
                out["product"] = line.split(":", 1)[1].strip()[:48]
            elif low.startswith("www-authenticate:") and "realm=" in low:
                out.setdefault("product", line.split("realm=", 1)[1]
                               .strip('" ')[:48])
        return out
    except Exception:                                     # noqa: BLE001
        return {}
    finally:
        try:
            s.close()
        except Exception:
            pass


def plain_tcp(ip: str, port: int, timeout: float):
    """The plant PC's own way of reaching a device: a normal socket."""
    return socket.create_connection((ip, port), timeout=timeout)


def describe(device: dict) -> str:
    """One line for the list: who made it and what it is."""
    vendor = str(device.get("vendor") or "").strip()
    product = str(device.get("product") or "").strip()
    dtype = str(device.get("type") or "").strip()
    if product and vendor:
        return "%s %s" % (vendor, product)
    if product:
        return product
    if vendor and dtype:
        return "%s %s" % (vendor, dtype)
    return vendor or dtype or ""


def identify(open_tcp, ip: str, ports) -> dict:
    """Best available identity for one device. Never raises."""
    ports = set(int(p) for p in (ports or []))
    out = {}
    if 44818 in ports:
        out = enet_ip(open_tcp, ip)
    if not out and (80 in ports or 443 in ports):
        out = http_banner(open_tcp, ip, 80 if 80 in ports else 443)
    return out
