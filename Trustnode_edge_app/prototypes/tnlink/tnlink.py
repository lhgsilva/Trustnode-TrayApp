# -*- coding: utf-8 -*-
"""TrustNode Link - remote access to the machine network. PROTOTYPE.

Two roles in one file, built as two exes:

    tnlink.exe   one binary, both jobs - it asks which this PC is,
                 or the tray does it for you (--tray)

The client pairs with the server using a Connection Code, and then reaches
the devices on the server's network: ping them, discover them, and map a
local port onto a remote device:port so an ordinary Windows program can
connect to it.

WHAT THIS IS NOT, AND WHY
-------------------------
It does NOT create a virtual network adapter, so Windows' own `ping.exe`
and Studio 5000's EtherNet/IP browse do not transparently see the remote
subnet. Doing that requires the OS to route foreign packets into our
tunnel, which on Windows means a signed kernel-mode TUN driver. There is no
userspace way to capture another process's traffic. That is a deliberate
boundary of this prototype, not an oversight - see README.md.

What it DOES give, with zero dependencies beyond what TrustNode already
ships, zero drivers, and no change to the PLC or the network:

  * ping   - ICMP echo issued from the server, with real RTT and TTL
  * scan   - sweep a /24 to find the PLCs, ifm blocks and meters
  * map    - a local TCP port forwarded to a remote device:port, so
             Studio 5000 (static-IP driver), a browser, or anything else
             connects normally

SECURITY
--------
  * X25519 ECDH for a fresh session key every connection, confirmed by an
    HMAC over the full handshake transcript keyed with the Connection Code.
    An eavesdropper cannot replay it and a man in the middle cannot sit in
    it without the code.
  * ChaCha20-Poly1305 AEAD, separate key and counter per direction. Every
    frame is authenticated; a tampered or reordered frame drops the link.
  * The server refuses any target outside an allowlist, which defaults to
    its OWN directly-connected private subnets. Without that the client
    could use the plant PC as an open proxy to the internet.
  * The session expires. Default 6 hours, then the server exits.
  * Every operation is logged to stdout with a timestamp.

This is a prototype: it is not wired into TrustNode, it holds one client at
a time, and it listens for an inbound TCP connection (so the first test is
a LAN or a port-forward, not an outbound-only plant link). The outbound-only
rendezvous through the VPS is the next step, not this one.
"""
from __future__ import annotations

import argparse
import ctypes
import ipaddress
import json
import os
import queue
import socket
import struct
import subprocess
import sys
import threading
import time
from ctypes import wintypes

from cryptography.hazmat.primitives import hashes, hmac as c_hmac
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = "tnlink-proto-0.1"

# 2026-10-28: an operator could not tell one build from the next - every
# exe said "tnlink-proto-0.1", so a stale copy on a desktop looked
# identical to a fresh one and a fix appeared not to have worked. build.py
# writes _build_stamp.py; running from source simply says "dev".
try:
    from _build_stamp import BUILD_STAMP          # type: ignore
except Exception:
    BUILD_STAMP = "dev"
VERSION_FULL = "%s  build %s" % (VERSION, BUILD_STAMP)
DEFAULT_PORT = 51820
MAX_FRAME = 1 << 20
CTRL, DATA, TUN = 1, 2, 3

# Ambiguous characters are left out so a code can be read over a phone.
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_CONFUSE = {"O": "0", "o": "0", "I": "1", "i": "1", "L": "1", "l": "1"}


def log(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# --------------------------------------------------------------------------
# Double-click survival
#
# 2026-10-21, reported: "when I tried to use the client exe it start and
# closed automatically". The client requires --server, so a double-click hit
# argparse's error path, which prints to stderr and exits - and the console
# window it had just created closed with the message still in it. Nothing
# was wrong with the build; the explanation was simply unreadable.
#
# GetConsoleProcessList tells us whether we are the ONLY process on this
# console. If we are, the window is ours and will vanish, so we hold it
# open. Run from an existing terminal the count is higher and nothing waits.
# Also gated on a tty, so a piped or scripted run can never hang.
# --------------------------------------------------------------------------
def _owns_console() -> bool:
    try:
        buf = (ctypes.c_uint * 8)()
        n = ctypes.windll.kernel32.GetConsoleProcessList(buf, 8)
        return int(n) <= 1
    except Exception:
        return False


def _interactive() -> bool:
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        return False


#: Set once the tray takes over. The console still exists - it is only
#: HIDDEN - so stdin is still a tty and the exit pause below still blocks
#: on it. 2026-10-28, reported: "the process need to be killed when we
#: close the app to avoid multiples istances". Quit stopped the service,
#: stopped the icon and returned cleanly from main() - and then sat for
#: ever on input() against a window nobody can see. Every run left a
#: process behind, and the next one started beside it.
_TRAY_MODE = False

#: Held for the life of the process, so the kernel releases it however
#: this process dies. Never closed deliberately.
_SINGLE_INSTANCE_HANDLE = None


def _other_instances() -> list:
    """PIDs of other TN-Link processes on this machine.

    Matched by image name, and only when we are the frozen exe. From
    source the process is called python.exe, and sweeping THAT would kill
    the developer's other work.
    """
    if not getattr(sys, "frozen", False):
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class _PE32(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD),
                        ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_char * 260)]

        k32 = ctypes.windll.kernel32
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)
        if snap == -1:
            return []
        me = os.getpid()
        # A one-file build is TWO processes of the same name: the
        # bootloader that unpacks, and the app it then runs. Measured
        # 2026-10-29 - a single start reported "TN-Link is already
        # running (pid 3912)" and refused ITSELF, so the exe could not be
        # started at all. Our own parent and our own children are us.
        try:
            parent = os.getppid()
        except Exception:
            parent = 0
        out = []
        try:
            e = _PE32()
            e.dwSize = ctypes.sizeof(_PE32)
            ok = k32.Process32First(snap, ctypes.byref(e))
            while ok:
                name = e.szExeFile.decode("mbcs", "replace").lower()
                pid = int(e.th32ProcessID)
                ppid = int(e.th32ParentProcessID)
                # Both variants are the same app: tnlink.exe and
                # tnlink-web.exe must not run beside each other either.
                if (name.startswith("tnlink") and name.endswith(".exe")
                        and pid != me and pid != parent and ppid != me):
                    out.append(pid)
                ok = k32.Process32Next(snap, ctypes.byref(e))
        finally:
            k32.CloseHandle(snap)
        return out
    except Exception:
        return []


def _claim_single_instance() -> bool:
    """True when THIS process is the only TN-Link on the machine.

    A lock FILE cannot do this job: a process that is killed leaves the
    file behind, and the operator is then told the app is running when it
    is not. A named mutex is released by the kernel whatever happens to
    the process.
    """
    global _SINGLE_INSTANCE_HANDLE
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = wintypes.HANDLE
        handle = k32.CreateMutexW(None, True, "Global\\TNLinkSingleInstance")
        if not handle:
            return True                 # cannot tell - never block the app
        if k32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
            return False
        _SINGLE_INSTANCE_HANDLE = handle
        return True
    except Exception:
        return True


def _pause() -> None:
    """Hold the window open on every interactive exit.

    2026-10-21: this was gated on _owns_console() too, which I tested under
    the classic console host. Under Windows Terminal the process count
    differs, the gate read False, and the window closed on an error exit -
    the exact symptom that was reported twice. Detecting a double-click is
    not reliable, so stop trying: pause whenever a human is at the keyboard.
    A pipe is not a tty, so automation still cannot hang.
    """
    if _TRAY_MODE:
        # The tray IS the window. There is nothing to hold open, and the
        # console it would ask on is hidden.
        return
    if _interactive():
        try:
            input("\n  Press Enter to close this window ...")
        except Exception:
            pass


# --------------------------------------------------------------------------
# Connection Code
# --------------------------------------------------------------------------
def new_code() -> str:
    import secrets
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(15))
    return "TN-%s-%s-%s" % (raw[0:5], raw[5:10], raw[10:15])


def normalise_code(text: str) -> str:
    """Be liberal in what we accept; a wrong code still fails.

    A pasted code survives spaces, tabs, line wrapping, lower case, a
    missing TN- prefix, and the hyphens a mail client has typographed into
    en/em dashes.
    """
    s = str(text or "")
    for dash in ("‐", "‑", "‒", "–", "—", "−"):
        s = s.replace(dash, "-")
    s = "".join(ch for ch in s if not ch.isspace())
    s = s.replace("-", "").replace("_", "")
    if s[:2].upper() == "TN":
        s = s[2:]
    s = "".join(_CONFUSE.get(ch, ch) for ch in s).upper()
    return s


# --------------------------------------------------------------------------
# ICMP, via iphlpapi - no admin rights and no locale-dependent parsing
# --------------------------------------------------------------------------
class _IcmpReply(ctypes.Structure):
    _fields_ = [("Address", ctypes.c_uint32),
                ("Status", ctypes.c_ulong),
                ("RoundTripTime", ctypes.c_ulong),
                ("DataSize", ctypes.c_ushort),
                ("Reserved", ctypes.c_ushort),
                ("Data", ctypes.c_void_p),
                ("Options_Ttl", ctypes.c_ubyte),
                ("Options_Tos", ctypes.c_ubyte),
                ("Options_Flags", ctypes.c_ubyte),
                ("Options_OptionsSize", ctypes.c_ubyte),
                ("Options_OptionsData", ctypes.c_void_p)]


_IPH = ctypes.windll.iphlpapi
_IPH.IcmpCreateFile.restype = wintypes.HANDLE
_IPH.IcmpCreateFile.argtypes = []
_IPH.IcmpCloseHandle.restype = wintypes.BOOL
_IPH.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
_IPH.IcmpSendEcho.restype = wintypes.DWORD
_IPH.IcmpSendEcho.argtypes = [wintypes.HANDLE, ctypes.c_uint32,
                              ctypes.c_char_p, ctypes.c_ushort,
                              ctypes.c_void_p, ctypes.c_void_p,
                              wintypes.DWORD, wintypes.DWORD]
_PROBE = b"trustnode-link-probe"
_BUF_SZ = ctypes.sizeof(_IcmpReply) + len(_PROBE) + 16


def icmp_once(ip: str, timeout_ms: int = 1200) -> dict:
    """One ICMP echo. Returns rtt_ms and ttl, or a reason."""
    try:
        addr = struct.unpack("<I", socket.inet_aton(ip))[0]
    except OSError:
        return {"ok": False, "reason": "not an IPv4 address"}
    h = _IPH.IcmpCreateFile()
    if not h:
        return {"ok": False, "reason": "IcmpCreateFile failed"}
    try:
        buf = ctypes.create_string_buffer(_BUF_SZ)
        n = _IPH.IcmpSendEcho(h, addr, _PROBE, len(_PROBE), None, buf,
                              _BUF_SZ, timeout_ms)
        if not n:
            err = ctypes.GetLastError()
            # 11010 IP_REQ_TIMED_OUT, 11003 IP_DEST_HOST_UNREACHABLE
            reason = {11010: "timed out", 11003: "host unreachable",
                      11002: "net unreachable"}.get(err, "error %d" % err)
            return {"ok": False, "reason": reason}
        r = ctypes.cast(buf, ctypes.POINTER(_IcmpReply)).contents
        if r.Status != 0:
            return {"ok": False, "reason": "status %d" % r.Status}
        return {"ok": True, "rtt_ms": int(r.RoundTripTime),
                "ttl": int(r.Options_Ttl)}
    finally:
        _IPH.IcmpCloseHandle(h)


def icmp_ping(ip: str, count: int = 4, timeout_ms: int = 1200) -> dict:
    out = []
    for i in range(max(1, min(int(count), 20))):
        if i:
            time.sleep(0.25)
        out.append(icmp_once(ip, timeout_ms))
    good = [r for r in out if r.get("ok")]
    res = {"ip": ip, "sent": len(out), "received": len(good),
           "replies": out}
    if good:
        rtts = [r["rtt_ms"] for r in good]
        res["rtt_min"] = min(rtts)
        res["rtt_max"] = max(rtts)
        res["rtt_avg"] = round(sum(rtts) / len(rtts), 1)
        res["ttl"] = good[0]["ttl"]
    return res


# --------------------------------------------------------------------------
# Local interfaces, from GetIpAddrTable - address AND mask, so we can derive
# the directly-connected subnets that form the default allowlist.
# --------------------------------------------------------------------------
class _IpAddrRow(ctypes.Structure):
    _fields_ = [("dwAddr", ctypes.c_uint32), ("dwIndex", ctypes.c_ulong),
                ("dwMask", ctypes.c_uint32), ("dwBCastAddr", ctypes.c_uint32),
                ("dwReasmSize", ctypes.c_ulong),
                ("unused1", ctypes.c_ushort), ("wType", ctypes.c_ushort)]


class _IpAddrString(ctypes.Structure):
    pass


_IpAddrString._fields_ = [("Next", ctypes.POINTER(_IpAddrString)),
                          ("IpAddress", ctypes.c_char * 16),
                          ("IpMask", ctypes.c_char * 16),
                          ("Context", ctypes.c_ulong)]


class _AdapterInfo(ctypes.Structure):
    pass


_AdapterInfo._fields_ = [
    ("Next", ctypes.POINTER(_AdapterInfo)),
    ("ComboIndex", ctypes.c_ulong),
    ("AdapterName", ctypes.c_char * 260),
    ("Description", ctypes.c_char * 132),
    ("AddressLength", ctypes.c_uint),
    ("Address", ctypes.c_ubyte * 8),
    ("Index", ctypes.c_ulong),
    ("Type", ctypes.c_uint),
    ("DhcpEnabled", ctypes.c_uint),
    ("CurrentIpAddress", ctypes.POINTER(_IpAddrString)),
    ("IpAddressList", _IpAddrString),
    ("GatewayList", _IpAddrString),
    ("DhcpServer", _IpAddrString),
    ("HaveWins", ctypes.c_int),
    ("PrimaryWinsServer", _IpAddrString),
    ("SecondaryWinsServer", _IpAddrString),
    ("LeaseObtained", ctypes.c_long),
    ("LeaseExpires", ctypes.c_long)]

# Two very different kinds of non-physical adapter, and conflating them
# was a real mistake. 2026-10-21: the operator and the engineer shared a
# VPN, and the tool labelled the VPN tunnel "usually NOT reachable from
# another PC" - which sent them to the Wi-Fi address instead, the one
# address that could not work. A hypervisor's switch exists only inside
# the PC. A VPN tunnel is exactly how a remote peer reaches you.
_LOCAL_ONLY_HINTS = ("virtualbox", "hyper-v", "vethernet", "vmware",
                     "loopback", "bluetooth", "wan miniport", "npcap",
                     "teredo", "isatap", "wsl", "docker", "vmnet")
_VPN_HINTS = ("wireguard", "openvpn", "nordlynx", "nordvpn", "tailscale",
              "zerotier", "tap-windows", "tap adapter", "wintun", "vpn",
              "anyconnect", "forticlient", "globalprotect", "pangp",
              "softether", "data channel offload", "tunnel")


def _kind(desc: str) -> str:
    low = (desc or "").lower()
    # VPN first: "WireGuard Tunnel" and "OpenVPN ... Adapter" would both
    # otherwise be caught by a generic 'virtual' test.
    if any(h in low for h in _VPN_HINTS):
        return "vpn"
    if any(h in low for h in _LOCAL_ONLY_HINTS):
        return "local"
    return "lan"


# The rendezvous. Baked in so the engineer types a Connection Code and
# nothing else; override at build time with TNLINK_RELAY, or at run time
# with --relay.
DEFAULT_RELAY = os.environ.get("TNLINK_RELAY", "trustnode.lsapps.app:7443")
R_MAGIC = b"TNLR1"
R_PING = bytes([0])
R_PAIRED = bytes([1])
R_NO_AGENT = bytes([2])
R_BUSY = bytes([3])
R_FULL = bytes([4])


def room_id(code_text: str) -> bytes:
    """What the relay matches on. Derived from the code, never the code.

    The relay is a byte pipe on someone else's machine; it has no business
    holding the secret that authenticates the session.
    """
    h = c_hmac.HMAC(normalise_code(code_text).encode(), hashes.SHA256())
    h.update(b"tnlink-room")
    return h.finalize()[:16]


def split_hostport(text: str, default_port: int) -> tuple:
    host = (text or "").strip()
    port = default_port
    if host.startswith("["):                      # bracketed IPv6
        end = host.find("]")
        if end > 0:
            if host[end + 1:end + 2] == ":":
                port = int(host[end + 2:])
            return host[1:end], port
    if ":" in host and host.count(":") == 1:
        host, p = host.rsplit(":", 1)
        port = int(p)
    return host.strip(), port


def relay_dial(relay: str, code: str, is_agent: bool,
               connect_timeout: float = 25.0) -> socket.socket:
    """Dial the rendezvous and wait to be paired. Outbound only."""
    host, port = split_hostport(relay, 7443)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(connect_timeout)
    try:
        s.connect((host, port))
    except OSError:
        s.close()
        raise
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.sendall(R_MAGIC + (b"a" if is_agent else b"c") + room_id(code))
    # An agent waits indefinitely; the relay pings so NAT keeps the path.
    s.settimeout(None if is_agent else 40.0)
    while True:
        b = _recv_exact(s, 1)
        if b == R_PING:
            continue
        if b == R_PAIRED:
            s.settimeout(None)
            return s
        s.close()
        raise AuthError({
            R_NO_AGENT: "no TrustNode Link server is waiting with that "
                        "Connection Code (is it running, and is the code "
                        "current?)",
            R_BUSY: "someone else is already connected to that server",
            R_FULL: "the relay is at capacity - try again shortly",
        }.get(b, "unexpected reply from the relay"))


FW_RULE = "TrustNode Link (prototype)"


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _fw_rule_exists(name: str = FW_RULE) -> bool:
    """netsh returns non-zero when no rule matches. Output is localised;
    the return code is not."""
    try:
        r = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=" + name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def _listening_exe() -> str:
    """The program Windows must be told to allow.

    Frozen, sys.executable IS the exe; as a script it is python.exe, which
    is genuinely the process that listens. Always absolute, both ways -
    the previous version used sys.argv[0], which can be relative.
    """
    return os.path.abspath(sys.executable)


def _fw_args(port: int, scope: str = "localsubnet") -> list:
    return ["netsh", "advfirewall", "firewall", "add", "rule",
            "name=" + FW_RULE, "dir=in", "action=allow",
            "program=" + _listening_exe(), "protocol=TCP",
            "localport=%d" % port, "profile=any", "remoteip=" + scope,
            "enable=yes"]


def _fw_command(port: int, scope: str = "localsubnet") -> str:
    """The same rule, as one line a person can paste into an admin prompt."""
    out = []
    for a in _fw_args(port, scope):
        if a.startswith(("name=", "program=")):
            k, v = a.split("=", 1)
            out.append('%s="%s"' % (k, v))
        else:
            out.append(a)
    return " ".join(out)


def _fw_add(port: int, scope: str = "localsubnet") -> tuple:
    if not _is_admin():
        return False, "needs an elevated prompt (Run as administrator)"
    try:
        r = subprocess.run(_fw_args(port, scope), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True,
                           errors="replace", timeout=30)
        if r.returncode == 0:
            return True, ("inbound TCP %d allowed for this program, from "
                          "the local subnet only" % port)
        return False, (r.stdout or "netsh returned %d" % r.returncode).strip()
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)


def _fw_remove() -> tuple:
    if not _is_admin():
        return False, "needs an elevated prompt (Run as administrator)"
    try:
        r = subprocess.run(["netsh", "advfirewall", "firewall", "delete",
                            "rule", "name=" + FW_RULE],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, errors="replace", timeout=30)
        if r.returncode == 0:
            return True, "rule removed"
        return False, (r.stdout or "netsh returned %d" % r.returncode).strip()
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)


def _primary_if_index() -> int:
    """Interface index of the adapter that reaches the outside world.

    Index, not name: a name is localised and quoted differently on every
    Windows, while PowerShell's -InterfaceIndex is neither.
    """
    want = primary_ip()
    size = ctypes.c_ulong(0)
    _IPH.GetAdaptersInfo(None, ctypes.byref(size))
    buf = ctypes.create_string_buffer(size.value)
    if _IPH.GetAdaptersInfo(buf, ctypes.byref(size)) != 0:
        return 0
    node = ctypes.cast(buf, ctypes.POINTER(_AdapterInfo))
    first = 0
    while node:
        a = node.contents
        addr = ctypes.pointer(a.IpAddressList)
        while addr:
            e = addr.contents
            ip = e.IpAddress.decode("ascii", "replace").strip()
            if ip and ip != "0.0.0.0" and not ip.startswith("127."):
                if not first:
                    first = int(a.Index)
                if ip == want:
                    return int(a.Index)
            addr = e.Next
        node = a.Next
    return first


def _run(args: list, timeout: int = 40) -> tuple:
    try:
        r = subprocess.run(args, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True,
                           errors="replace", timeout=timeout)
        return r.returncode == 0, (r.stdout or "").strip()
    except Exception as exc:                                # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)


def _ps(cmd: str, timeout: int = 40) -> tuple:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", cmd],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, errors="replace", timeout=timeout)
        return r.returncode == 0, (r.stdout or "").strip()
    except Exception as exc:                                # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)


def _interface_facts() -> dict:
    """{ifIndex: {"dhcp": bool, "default_route": bool, "name": str}}.

    Asked of Windows itself rather than guessed, because getting this
    wrong took a live machine off the internet.
    """
    ok, out = _ps(
        "$d=@{}; Get-NetIPInterface -AddressFamily IPv4 | "
        "ForEach-Object { $d[[string]$_.InterfaceIndex]=@{} };"
        "Get-NetIPInterface -AddressFamily IPv4 | ForEach-Object {"
        " $r = Get-NetRoute -InterfaceIndex $_.InterfaceIndex "
        "-DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue;"
        " '{0}|{1}|{2}|{3}' -f $_.InterfaceIndex, $_.Dhcp, "
        "[bool]$r, $_.InterfaceAlias }")
    facts = {}
    if not ok:
        return facts
    for line in (out or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) != 4 or not parts[0].isdigit():
            continue
        facts[int(parts[0])] = {
            "dhcp": parts[1].strip().lower() == "enabled",
            "default_route": parts[2].strip().lower() == "true",
            "name": parts[3].strip()}
    return facts


def pick_claim_interface() -> tuple:
    """An adapter it is SAFE to borrow an address on.

    Returns (index, name, reason-if-none).
    """
    facts = _interface_facts()
    if not facts:
        return 0, "", "could not read this PC's interface configuration"
    safe = [(i, f) for i, f in facts.items()
            if not f["dhcp"] and not f["default_route"]
            and "loopback" not in f["name"].lower()]
    if not safe:
        risky = sorted(
            "%s (%s)" % (f["name"], "DHCP" if f["dhcp"] else "default route")
            for f in facts.values() if f["dhcp"] or f["default_route"])
        return 0, "", ("every adapter here is DHCP-managed or carries the "
                       "default route: %s" % ", ".join(risky[:4]))
    # Prefer a virtual adapter - borrowing an address there disturbs
    # nothing real.
    def score(item):
        name = item[1]["name"].lower()
        return (0 if any(h in name for h in ("vmware", "vmnet", "hyper-v",
                                             "vethernet", "virtualbox",
                                             "loopback")) else 1,
                item[0])
    safe.sort(key=score)
    idx, f = safe[0]
    return idx, f["name"], ""


def _add_local_ip(if_index: int, ip: str) -> tuple:
    """Give this PC the device's own address, as a /32 host address.

    store=active is the important part: the address lives until the next
    reboot and no longer. New-NetIPAddress writes the PERSISTENT store as
    well, so a crash would leave someone else's IP hijacked on the
    engineer's laptop forever. netsh takes the interface INDEX here, which
    avoids the localised adapter name entirely.
    """
    ok, out = _run(["netsh", "interface", "ipv4", "add", "address",
                    "name=%d" % if_index, "addr=%s" % ip,
                    "mask=255.255.255.255", "skipassource=true",
                    "store=active"])
    if not ok and "skipassource" in (out or "").lower():
        # Older Windows builds do not know the flag; the address is still
        # correct without it.
        ok, out = _run(["netsh", "interface", "ipv4", "add", "address",
                        "name=%d" % if_index, "addr=%s" % ip,
                        "mask=255.255.255.255", "store=active"])
    if ok:
        return True, ""
    low = (out or "").lower()
    if "already" in low or "exists" in low:
        return True, ""
    return False, (out.splitlines()[0][:150] if out else "unknown error")


def _remove_local_ip(ip: str) -> tuple:
    ok, out = _ps("Remove-NetIPAddress -IPAddress %s -Confirm:$false "
                  "-ErrorAction Stop" % ip)
    if ok:
        return True, ""
    return False, out.splitlines()[0][:150] if out else "unknown error"


def primary_ip() -> str:
    """The address Windows would use to reach the outside world.

    Connecting a UDP socket sends nothing; it just makes the stack pick a
    route, which is the most reliable way to find the real LAN address.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("203.0.113.9", 9))        # TEST-NET-3, no packet is sent
        return s.getsockname()[0]
    except Exception:
        return ""
    finally:
        s.close()


def local_subnets() -> list:
    """Every directly-connected IPv4 subnet, with the adapter it belongs to.

    The adapter description is what lets the operator tell a real network
    card from a hypervisor's private switch.
    """
    size = ctypes.c_ulong(0)
    _IPH.GetAdaptersInfo(None, ctypes.byref(size))
    buf = ctypes.create_string_buffer(size.value)
    out = []
    if _IPH.GetAdaptersInfo(buf, ctypes.byref(size)) == 0:
        node = ctypes.cast(buf, ctypes.POINTER(_AdapterInfo))
        while node:
            a = node.contents
            desc = a.Description.decode("mbcs", "replace").strip()
            gw = a.GatewayList.IpAddress.decode("ascii", "replace").strip()
            addr = ctypes.pointer(a.IpAddressList)
            while addr:
                e = addr.contents
                ip = e.IpAddress.decode("ascii", "replace").strip()
                mask = e.IpMask.decode("ascii", "replace").strip()
                if ip and ip != "0.0.0.0" and not ip.startswith("127."):
                    try:
                        net = ipaddress.ip_network("%s/%s" % (ip, mask),
                                                   strict=False)
                        kind = _kind(desc)
                        out.append({
                            "ip": ip, "mask": mask, "cidr": str(net),
                            "desc": desc or "network adapter",
                            "gateway": gw if gw and gw != "0.0.0.0" else "",
                            "kind": kind, "virtual": kind != "lan"})
                    except ValueError:
                        pass
                addr = e.Next
            node = a.Next
    if out:
        return out

    # Fallback: the address table, with no adapter names.
    size = ctypes.c_ulong(0)
    _IPH.GetIpAddrTable(None, ctypes.byref(size), 0)
    buf = ctypes.create_string_buffer(size.value)
    if _IPH.GetIpAddrTable(buf, ctypes.byref(size), 0) != 0:
        return []
    n = struct.unpack("<I", buf.raw[:4])[0]
    rows = ctypes.cast(ctypes.byref(buf, 4),
                       ctypes.POINTER(_IpAddrRow * n)).contents
    for r in rows:
        if not r.dwAddr or not r.dwMask:
            continue
        ip = socket.inet_ntoa(struct.pack("<I", r.dwAddr))
        mask = socket.inet_ntoa(struct.pack("<I", r.dwMask))
        if ip.startswith("127."):
            continue
        try:
            net = ipaddress.ip_network("%s/%s" % (ip, mask), strict=False)
        except ValueError:
            continue
        out.append({"ip": ip, "mask": mask, "cidr": str(net),
                    "desc": "network adapter", "gateway": "",
                    "kind": "lan", "virtual": False})
    return out


# --------------------------------------------------------------------------
# Transport: X25519 + PSK confirmation, then ChaCha20-Poly1305 per frame
# --------------------------------------------------------------------------
class AuthError(Exception):
    pass


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return buf


def _mac(code: bytes, transcript: bytes, label: bytes) -> bytes:
    h = c_hmac.HMAC(code, hashes.SHA256())
    h.update(label + b"|" + transcript)
    return h.finalize()


def _verify(code: bytes, transcript: bytes, label: bytes, tag: bytes) -> None:
    h = c_hmac.HMAC(code, hashes.SHA256())
    h.update(label + b"|" + transcript)
    try:
        h.verify(tag)
    except Exception:
        raise AuthError("the Connection Code does not match")


class Secure:
    """Length-prefixed AEAD frames over a TCP socket.

    One key and one counter per direction. The counter never repeats within
    a session, and a session key is never reused across connections, so the
    nonce can be a plain counter.
    """

    def __init__(self, sock: socket.socket, send_key: bytes, recv_key: bytes):
        self.sock = sock
        self._tx = ChaCha20Poly1305(send_key)
        self._rx = ChaCha20Poly1305(recv_key)
        self._tx_n = 0
        self._rx_n = 0
        self._tx_lock = threading.Lock()
        self._rx_lock = threading.Lock()

    @staticmethod
    def _nonce(counter: int) -> bytes:
        return b"\x00\x00\x00\x00" + struct.pack(">Q", counter)

    def send(self, plaintext: bytes) -> None:
        with self._tx_lock:
            ct = self._tx.encrypt(self._nonce(self._tx_n), plaintext, None)
            self._tx_n += 1
            self.sock.sendall(struct.pack(">I", len(ct)) + ct)

    def recv(self) -> bytes:
        with self._rx_lock:
            (n,) = struct.unpack(">I", _recv_exact(self.sock, 4))
            if n > MAX_FRAME:
                raise AuthError("frame too large (%d)" % n)
            ct = _recv_exact(self.sock, n)
            pt = self._rx.decrypt(self._nonce(self._rx_n), ct, None)
            self._rx_n += 1
            return pt

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


def handshake(sock: socket.socket, code_text: str, is_server: bool) -> Secure:
    """Authenticated key exchange. Fresh keys per connection.

    The ECDH gives forward secrecy; the HMAC over the transcript, keyed with
    the Connection Code, is what stops a man in the middle substituting its
    own public key.
    """
    code = normalise_code(code_text).encode()
    if len(code) < 10:
        raise AuthError("the Connection Code is too short to be safe")

    priv = X25519PrivateKey.generate()
    mine = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)

    hello = VERSION.encode().ljust(24, b"\x00")
    if is_server:
        sock.sendall(hello + mine)
        peer_hello = _recv_exact(sock, 24)
        theirs = _recv_exact(sock, 32)
        s_pub, c_pub = mine, theirs
    else:
        peer_hello = _recv_exact(sock, 24)
        theirs = _recv_exact(sock, 32)
        sock.sendall(hello + mine)
        s_pub, c_pub = theirs, mine

    peer_ver = peer_hello.rstrip(b"\x00").decode("ascii", "replace")
    if peer_ver != VERSION:
        raise AuthError("version mismatch: peer is %r, this is %r"
                        % (peer_ver, VERSION))

    shared = priv.exchange(X25519PublicKey.from_public_bytes(theirs))

    transcript = VERSION.encode() + s_pub + c_pub
    keys = HKDF(algorithm=hashes.SHA256(), length=64,
                salt=transcript, info=b"tnlink-v1-keys").derive(shared)
    s2c, c2s = keys[:32], keys[32:]

    # Each side proves it holds the code, over the exact transcript it saw.
    if is_server:
        sock.sendall(_mac(code, transcript, b"server"))
        # The client checks OUR proof first and hangs up if it fails, so a
        # wrong code reaches us as a plain disconnect. Logged raw, that is
        # indistinguishable from a network blip - and "someone is trying
        # codes at your machine" is exactly what the operator should see.
        try:
            tag = _recv_exact(sock, 32)
        except ConnectionError:
            raise AuthError("peer hung up at code confirmation - most "
                            "likely a wrong Connection Code")
        _verify(code, transcript, b"client", tag)
        return Secure(sock, s2c, c2s)
    _verify(code, transcript, b"server", _recv_exact(sock, 32))
    sock.sendall(_mac(code, transcript, b"client"))
    return Secure(sock, c2s, s2c)


# --------------------------------------------------------------------------
# Frame bodies
# --------------------------------------------------------------------------
def pack_ctrl(obj: dict) -> bytes:
    return bytes([CTRL]) + json.dumps(obj).encode()


def pack_data(sid: int, payload: bytes) -> bytes:
    return bytes([DATA]) + struct.pack(">I", sid) + payload


def pack_tun(packet: bytes) -> bytes:
    """One raw IP packet, straight off a TN-Link adapter."""
    return bytes([TUN]) + packet


def parse(frame: bytes):
    if not frame:
        raise AuthError("empty frame")
    kind = frame[0]
    if kind == CTRL:
        return CTRL, json.loads(frame[1:].decode()), None
    if kind == DATA:
        (sid,) = struct.unpack(">I", frame[1:5])
        return DATA, sid, frame[5:]
    if kind == TUN:
        return TUN, None, frame[1:]
    raise AuthError("unknown frame type %d" % kind)


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
class Server:
    def __init__(self, code: str, allow: list, port: int, minutes: int,
                 bind: str, firewall: bool = True, relay: str = ""):
        self.code = code
        self.port = port
        self.bind = bind
        self.firewall = firewall
        self.relay = relay
        self.deadline = time.time() + minutes * 60 if minutes > 0 else 0
        # What the operator typed with --allow, kept so a refresh can tell an
        # explicit choice from the default "every private subnet I can see".
        self._allow_arg = list(allow or [])
        self.subnets = local_subnets()
        nets = self._compute_allow()
        # Two local addresses on one subnet listed it twice, which made the
        # allowlist look wider than it is. The operator reads this line to
        # decide whether to hand over the code.
        self.allow = sorted(set(nets), key=lambda n: (n.network_address, n.prefixlen))
        self.streams = {}
        self.tunnel = None
        # 2026-10-28. The tray asked "is anybody paired?" by looking for
        # `sec`, because that is how Client says so - but only Client had
        # it, so every PLANT reported nobody connected while an engineer
        # was working through it. The server now sets the same attribute
        # around the session it is actually serving: one name, both roles.
        self.sec = None
        self._plant_ip = ""
        self._plant_guard = None
        self.lock = threading.Lock()

    def _compute_allow(self) -> list:
        """The networks the engineer may reach, from what is here NOW."""
        nets = [ipaddress.ip_network(c, strict=False) for c in self._allow_arg]
        if not nets:
            nets = [ipaddress.ip_network(s["cidr"], strict=False)
                    for s in self.subnets
                    if ipaddress.ip_network(s["cidr"], strict=False).is_private]
        return nets

    def refresh_subnets(self) -> bool:
        """Re-read the adapters. True when the allowlist changed.

        2026-10-22, from a live session: local_subnets() ran ONCE, in
        __init__. An operator who started the server and then plugged in the
        PLC - or whose machine brought its second NIC up a moment later - had
        that network invisible for the whole session. The banner showed only
        the Wi-Fi subnet, the PLC on 192.168.0.1 was outside the allowlist,
        and every option that needed it was refused with no explanation that
        pointed at the cause.

        An operator who named networks with --allow keeps exactly those: a
        refresh must never widen a choice someone made deliberately.
        """
        before = list(self.allow)
        self.subnets = local_subnets()
        self.allow = sorted(set(self._compute_allow()),
                            key=lambda n: (n.network_address, n.prefixlen))
        return [str(n) for n in before] != [str(n) for n in self.allow]

    # -- policy -----------------------------------------------------------
    def permitted(self, ip: str) -> tuple:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False, "not an IPv4 address"
        if a.is_loopback or a.is_multicast or a.is_link_local:
            return False, "loopback, multicast and link-local are refused"
        for n in self.allow:
            if a in n:
                return True, ""
        return False, ("%s is outside the allowlist (%s)"
                       % (ip, ", ".join(str(n) for n in self.allow)))

    # -- what the operator reads ------------------------------------------
    def _ranked(self) -> list:
        """Best address to hand out first; private virtual ones last.

        The operator has no way to know that 192.168.56.1 is a hypervisor's
        private switch and 192.168.0.161 is the office LAN. Rank them, name
        the adapter, and say plainly which one to give out.
        """
        prim = primary_ip()

        def rank(s):
            k = s.get("kind", "lan")
            return (0 if s["ip"] == prim else
                    1 if (k == "lan" and s["gateway"]) else
                    2 if k == "lan" else
                    3 if k == "vpn" else 4)
        return sorted(self.subnets, key=lambda s: (rank(s), s["ip"]))

    def _secret_for(self, peer_unit: str):
        """(secret, came_from_trust). A known engineer skips the code."""
        try:
            if peer_unit and is_trusted(peer_unit):
                for r in trusted_units():
                    if str(r.get("unit") or "").upper() == str(peer_unit).upper():
                        # The stored value is a HASH; the shared secret for
                        # the handshake is derived from it, so the key itself
                        # is never kept in the clear on this side either.
                        return "tnlink-trust:" + str(r.get("hash") or ""), True
        except Exception:
            pass
        return self.code, False

    def pairing_key(self, host: str) -> str:
        return "%s:%d#%s" % (host, self.port, self.code)

    def _banner_relay(self) -> None:
        print()
        print("  TN-Link - server  (%s)" % VERSION_FULL)
        print("  " + "=" * 64)
        print()
        print("    UNIT ID : %s   (this PC, always the same)" % unit_id())
        print()
        print("    GIVE THIS CODE TO THE ENGINEER:")
        print()
        print("        %s" % self.code)
        print()
        print("    That is all. No address, no firewall rule, no port.")
        print("    This PC dials out to the relay and waits there.")
        print()
        print("  " + "=" * 64)
        lan = [a for a in self._ranked() if a.get("kind") == "lan"]
        if lan:
            print("  Devices on   : %s"
                  % ", ".join("%s (%s)" % (a["cidr"], a["desc"][:26])
                              for a in lan))
        print("  Allowed      : %s" % ", ".join(str(n) for n in self.allow))
        if self.deadline:
            print("  Expires      : in %d minute(s)"
                  % round((self.deadline - time.time()) / 60))
        print("  " + "-" * 64)
        print("  Nothing is reachable until the engineer pairs, and only")
        print("  the subnets above ever are. The relay carries bytes it")
        print("  cannot read - the session is encrypted end to end.")
        print("  Close this window to end the session.")
        print()
        sys.stdout.flush()

    def _banner(self) -> None:
        if self.relay:
            return self._banner_relay()
        ranked = self._ranked()
        best = ranked[0]["ip"] if ranked else "<this-pc-address>"
        print()
        print("  TN-Link - server  (%s)" % VERSION_FULL)
        print("  " + "=" * 64)
        print()
        print("    UNIT ID : %s   (this PC, always the same)" % unit_id())
        print()
        print("    GIVE THIS ONE LINE TO THE ENGINEER:")
        print()
        print("        %s" % self.pairing_key(best))
        print()
        print("    They paste it into tnlink.exe. Nothing else to type.")
        print()
        print("  " + "=" * 64)
        if len(ranked) > 1:
            print("  If that address does not work, this PC is also on:")
            for s in ranked[1:]:
                note = {"lan": "another network this PC is on",
                        "vpn": "VPN TUNNEL - use this one if the engineer "
                               "is on the same VPN",
                        "local": "exists only inside this PC - not "
                                 "reachable from anywhere"}[s.get("kind", "lan")]
                print("    %-15s %-30s %s"
                      % (s["ip"], s["desc"][:30], note))
            print("    (use the same line, with the address swapped)")
            if any(s.get("kind") == "vpn" for s in ranked[1:]):
                print()
                print("    If the Wi-Fi/LAN address times out but you both")
                print("    sit on a VPN, use the VPN TUNNEL address. A VPN")
                print("    client can also swallow LAN traffic, which looks")
                print("    exactly like a firewall block.")
            print("  " + "-" * 64)
        print("  Listening on    : %s:%d" % (self.bind, self.port))
        print("  Devices allowed : %s"
              % ", ".join(str(n) for n in self.allow))
        if self.deadline:
            print("  Session expires : in %d minute(s)"
                  % round((self.deadline - time.time()) / 60))
        print("  " + "-" * 64)
        print("  Nothing is reachable until the engineer pairs, and only the")
        print("  subnets above ever are. Close this window to end the session.")
        print()
        self._firewall_notice()
        # Redirected to a file or a pipe, stdout is block-buffered on
        # Windows, so the operator's instructions sat unwritten.
        sys.stdout.flush()

    def _ensure_firewall(self) -> None:
        """Running as administrator means consent to fix this.

        Asked for 2026-10-21: "we should have the firewall rules installed
        when we run the app as admin". Without the rule Windows drops the
        connection silently and the engineer sees only 'timed out'. So if
        we are elevated and the rule is missing, add it - scoped to this
        program and the local subnet, never the internet. Unelevated we
        change nothing and the banner explains what to do.
        """
        if _fw_rule_exists():
            return
        if not _is_admin():
            return
        ok, why = _fw_add(self.port)
        if ok:
            log("firewall rule added - %s" % why)
        else:
            log("firewall rule NOT added - %s" % why)

    def _firewall_notice(self) -> None:
        """Say the thing that is actually stopping the connection.

        2026-10-21: the client reported 'timed out' while the server was
        listening correctly on 0.0.0.0. The machine's networks were all
        categorised Public, and Windows Firewall drops unsolicited inbound
        on Public by default - so the SYN never reached this process. There
        was no rule for the port and nothing anywhere said so.

        'timed out' means dropped. 'actively refused' means nothing is
        listening. They are different faults and must not read the same.
        """
        if not self.firewall:
            print("  Firewall        : not touched (--no-firewall).")
            print()
            return
        if _fw_rule_exists():
            print("  Firewall        : inbound TCP %d is allowed for this"
                  % self.port)
            print("                    program, local subnet only.")
            print("                    Remove it later with:")
            print("                      tnlink.exe --remove-firewall")
            print("                    (as administrator)")
            print()
            sys.stdout.flush()
            return
        print("  " + "!" * 64)
        print("  WINDOWS FIREWALL WILL PROBABLY BLOCK THIS")
        print()
        print("  There is no inbound rule for TCP %d, so Windows drops the"
              % self.port)
        print("  connection silently and the engineer sees 'timed out'")
        print("  rather than anything useful.")
        print()
        if _is_admin():
            print("  This window IS elevated but the rule could not be")
            print("  added - the line above says why. Add it by hand:")
            print()
            print("      %s" % _fw_command(self.port))
        else:
            print("  Fix it in ONE of these ways:")
            print()
            print("   a) close this window, then right-click")
            print("      tnlink.exe -> Run as administrator.")
            print("      The rule is added automatically when elevated.")
            print()
            print("   b) or run this once in an admin Command Prompt:")
            print()
            print("      %s" % _fw_command(self.port))
        print()
        print("  The rule allows only the LOCAL SUBNET, not the internet.")
        print("  (The production design is outbound-only and needs no rule")
        print("   at all - this is a limitation of the prototype.)")
        print("  " + "!" * 64)
        print()
        sys.stdout.flush()

    def _serve_relay(self) -> int:
        """Dial out, wait to be paired, serve, repeat.

        No listening socket, so no firewall rule, no port-forward and no
        administrator rights - at either end. This is the only shape that
        can be deployed at a customer site.
        """
        backoff = 2.0
        while True:
            if self.deadline and time.time() > self.deadline:
                log("session expired - closing")
                return 0
            try:
                sock = relay_dial(self.relay, self.code, is_agent=True)
                backoff = 2.0
            except AuthError as exc:
                log("relay refused: %s" % exc)
                return 2
            except OSError as exc:
                log("cannot reach the relay %s (%s) - retrying in %.0fs"
                    % (self.relay, exc.strerror or exc, backoff))
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            log("an engineer connected through the relay")
            try:
                # 2026-10-31. Who is calling? The unit id arrives before the
                # handshake so this end can pick the right secret: a key we
                # minted for an engineer we already trust, or - for anyone
                # new - the one-shot code. The KEY itself never crosses the
                # wire; only the id it is filed under, which is an opaque
                # label, not a secret.
                # 2026-10-31: a unit-id preamble was briefly sent here so each
                # end could pick a per-pair key instead of a code. It broke
                # interoperability - a new client could not pair with a
                # server still on the old build, and the engineer trying to
                # reach a customer saw the app die on connect. Reverted.
                # Pairing once is still the right feature; it needs to be
                # negotiated INSIDE the existing version handshake so an old
                # peer is detected and fallen back to, not bolted on in
                # front of it.
                sec = handshake(sock, self.code, is_server=True)
            except AuthError as exc:
                log("REFUSED - %s" % exc)
                try:
                    sock.close()
                except OSError:
                    pass
                continue
            except (ConnectionError, OSError) as exc:
                log("pairing failed (%s)" % type(exc).__name__)
                continue
            log("PAIRED")
            self.sec = sec
            try:
                self._session(sec)
            except (ConnectionError, OSError) as exc:
                log("engineer disconnected (%s)" % type(exc).__name__)
            except AuthError as exc:
                log("LINK DROPPED - %s" % exc)
            except Exception as exc:                        # noqa: BLE001
                log("LINK DROPPED - %s: %s (frame rejected)"
                    % (type(exc).__name__, exc))
            finally:
                self.sec = None
                self._drop_all()
                sec.close()
                # 2026-10-22: BURN THE CODE. It used to loop straight back
                # and wait again on the SAME code, so one code served any
                # number of sessions for as long as the window lasted - and
                # a server forgotten on an old machine kept answering it.
                # An engineer paired with yesterday's computer and worked
                # there for forty minutes without knowing.
                #
                # A connection code is now good for exactly one session. The
                # next engineer needs a code the operator hands over
                # deliberately, which is the whole point of handing it over.
                # 2026-10-31, requested: "a way to reconnect to the same
                # computer after disconnect without re-entering the
                # connection id code".
                #
                # The code is still burned after one session - that property
                # is the whole point above and it stays. What changes is WHO
                # learns the next one. At pairing, over the already-encrypted
                # channel, this server tells the engineer it just let in what
                # its next code will be. So:
                #
                #   * the engineer who was already paired reconnects silently;
                #   * somebody who merely overheard the old code cannot - it
                #     is spent;
                #   * a server forgotten on an old machine still stops
                #     answering the code it gave out;
                #   * "New code" in the menu still revokes everything, because
                #     it replaces the chain.
                #
                # No wire change: an older client simply ignores the extra
                # field and keeps asking its operator for a code.
                old_code = self.code
                self.code = getattr(self, "next_code", "") or new_code()
                self.next_code = ""
                log("session closed - the code %s is now dead" % old_code)
                log("")
                log("  GIVE THIS NEW CODE TO THE NEXT ENGINEER:")
                log("      %s" % self.code)
                log("")

    # -- lifecycle --------------------------------------------------------
    def serve(self) -> int:
        # Relay mode binds NOTHING. This branch has to come before the
        # socket is created: it used to sit after the bind, so a relay-mode
        # server still tried to listen locally and died with "only one
        # usage of each socket address" when another instance held the
        # port - a port it had no business touching.
        if self.relay:
            self._banner()
            return self._serve_relay()

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # NOT SO_REUSEADDR. On Windows that lets a SECOND process bind a
        # port another process is already listening on, and which one gets
        # a given connection is undefined. Measured 2026-10-21: two servers
        # sat on 0.0.0.0:51820, each showing a different Connection Code,
        # and a client reaching "the server" hit whichever won the race.
        # SO_EXCLUSIVEADDRUSE makes the second bind fail honestly instead.
        try:
            srv.setsockopt(socket.SOL_SOCKET,
                           getattr(socket, "SO_EXCLUSIVEADDRUSE", 0xFFFFFFFB), 1)
        except OSError:
            pass
        try:
            srv.bind((self.bind, self.port))
        except OSError as exc:
            srv.close()
            print()
            print("  Cannot listen on %s:%d - %s"
                  % (self.bind, self.port, exc.strerror or exc))
            print("  Another TN-Link server is almost certainly")
            print("  already running. Close its window, or start this one")
            print("  with a different port:  --port %d" % (self.port + 1))
            print()
            return 2
        srv.listen(4)
        srv.settimeout(1.0)

        if self.firewall:
            self._ensure_firewall()
        self._banner()

        try:
            while True:
                if self.deadline and time.time() > self.deadline:
                    log("session expired - closing")
                    return 0
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                log("connection from %s:%d" % addr)
                try:
                    sec = handshake(conn, self.code, is_server=True)
                except AuthError as exc:
                    log("REFUSED %s:%d - %s" % (addr[0], addr[1], exc))
                    conn.close()
                    continue
                except Exception as exc:
                    log("handshake failed from %s:%d - %r"
                        % (addr[0], addr[1], exc))
                    conn.close()
                    continue
                log("PAIRED with %s:%d" % addr)
                self.sec = sec
                try:
                    self._session(sec)
                except (ConnectionError, OSError) as exc:
                    log("client disconnected (%s)" % type(exc).__name__)
                except AuthError as exc:
                    log("LINK DROPPED - %s" % exc)
                except Exception as exc:
                    # A forged or corrupted frame raises InvalidTag out of
                    # the AEAD. Found by the self-test: it propagated out of
                    # serve() and killed the whole server, so one bad frame
                    # ended the session for everyone. Drop the client, keep
                    # serving.
                    log("LINK DROPPED - %s: %s (frame rejected)"
                        % (type(exc).__name__, exc))
                finally:
                    self.sec = None
                    self._drop_all()
                    sec.close()
                    log("session closed")
        except KeyboardInterrupt:
            print()
            log("stopped")
            return 0
        finally:
            srv.close()

    def _drop_all(self) -> None:
        # A dropped session must not leave an adapter, a NAT rule or a
        # forwarding flag behind on the plant PC.
        if self.tunnel:
            try:
                t, self.tunnel = self.tunnel, None
                (t.stop if hasattr(t, "stop") else t.down)()
            except Exception:                               # noqa: BLE001
                pass
        with self.lock:
            for s in list(self.streams.values()):
                try:
                    s.close()
                except Exception:
                    pass
            self.streams.clear()

    # -- per-connection ---------------------------------------------------
    def _session(self, sec: Secure) -> None:
        while True:
            if self.deadline and time.time() > self.deadline:
                sec.send(pack_ctrl({"op": "bye", "reason": "session expired"}))
                log("session expired - dropping client")
                return
            kind, a, b = parse(sec.recv())
            if kind == TUN:
                t = self.tunnel
                if t is not None:
                    # The netstack takes packets; the bridge endpoint
                    # takes frames. Both arrive the same way.
                    (t.on_packet if hasattr(t, "on_packet")
                     else t.inject)(b)
                continue
            if kind == DATA:
                with self.lock:
                    s = self.streams.get(a)
                if s:
                    try:
                        s.sendall(b)
                    except OSError:
                        self._close_stream(sec, a, "write failed")
                continue
            self._control(sec, a)

    def _control(self, sec: Secure, msg: dict) -> None:
        op = str(msg.get("op") or "")
        if op == "hello":
            # Re-read the adapters before answering. The engineer asks this
            # the moment they pair, which is exactly when the operator has
            # finished plugging things in - and the list built at process
            # start may predate the PLC's own network. See refresh_subnets().
            try:
                if self.refresh_subnets():
                    log("networks re-read: now sharing %s"
                        % (", ".join(str(n) for n in self.allow) or "nothing"))
            except Exception as exc:
                log("could not re-read the networks: %s" % exc)
            # Mint the next code now and hand it to this engineer only.
            # Generated once per session so repeated hellos do not keep
            # changing what the far end has stored.
            if not getattr(self, "next_code", ""):
                self.next_code = new_code()
            sec.send(pack_ctrl({"op": "hello", "version": VERSION,
                                # What this engineer should use next time.
                                # An older client ignores it.
                                "next_code": self.next_code,
                                # 2026-10-31: the permanent id, so the other
                                # end can file this machine in its address
                                # book under something that outlives the
                                # one-shot pairing code.
                                "unit": unit_id(),
                                "host": socket.gethostname(),
                                "subnets": self.subnets,
                                "allow": [str(n) for n in self.allow],
                                "expires_in_s": (
                                    int(self.deadline - time.time())
                                    if self.deadline else 0)}))
            return

        if op == "ping":
            ip = str(msg.get("ip") or "")
            ok, why = self.permitted(ip)
            if not ok:
                log("ping %s REFUSED - %s" % (ip, why))
                sec.send(pack_ctrl({"op": "ping", "error": why}))
                return
            res = icmp_ping(ip, int(msg.get("count") or 4),
                            int(msg.get("timeout_ms") or 1200))
            log("ping %s -> %d/%d" % (ip, res["received"], res["sent"]))
            res["op"] = "ping"
            sec.send(pack_ctrl(res))
            return

        if op == "bridge_up":
            self._bridge_up(sec, msg)
            return

        if op == "bridge_down":
            self._bridge_down(sec)
            return

        if op == "tunnel_up":
            self._tunnel_up(sec, msg)
            return

        if op == "tunnel_down":
            self._tunnel_down(sec)
            return

        if op == "scan":
            self._scan(sec, msg)
            return

        if op == "open":
            self._open(sec, msg)
            return

        if op == "close":
            self._close_stream(sec, int(msg.get("sid") or 0),
                               "closed by client", tell=False)
            return

        sec.send(pack_ctrl({"op": "error",
                            "error": "unknown op %r" % op}))

    def _bridge_up(self, sec: Secure, msg: dict) -> None:
        """Share this PC's plant network with the engineer, by bridging.

        The remote PC joins the plant Ethernet segment, so the PLC answers
        it by itself. Nothing here cares which addresses the PLC or this
        PC use - that is the whole point of doing it at Layer 2.
        """
        import tnlink_bridge as B
        if self.tunnel:
            sec.send(pack_ctrl({"op": "bridge_up", "error":
                                "something is already up on this agent"}))
            return
        offered = [str(n) for n in self.allow]
        plant_cidr = msg.get("subnet") or (offered[0] if offered else "")
        # One PowerShell call answers all of it.
        info = B.survey(plant_cidr, B.AGENT_NAME) if plant_cidr else {}
        name = info.get("plant_adapter", "")
        ip_pfx = info.get("plant", "")
        if not name:
            sec.send(pack_ctrl({"op": "bridge_up", "error":
                                "no adapter on %s" % plant_cidr}))
            return
        log("BRIDGE requested - plant adapter %s (%s)" % (name, ip_pfx))

        # Open the adapter FIRST. A TAP adapter reads "cable unplugged"
        # until a program holds its handle, and Windows refuses to put a
        # disconnected adapter into a bridge - so checking the bridge
        # before opening it made the one-time step impossible to complete.
        if self.tunnel is None:
            try:
                ep = B.L2Endpoint(lambda f: sec.send(pack_tun(f)),
                                  name=B.AGENT_NAME, keep_adapter=True,
                                  filter_flood=True)
                ep.up()
                self.tunnel = ep
            except Exception as exc:                        # noqa: BLE001
                log("BRIDGE FAILED - %s: %s" % (type(exc).__name__, exc))
                sec.send(pack_ctrl({"op": "bridge_up", "error": str(exc)}))
                return
        log("  %s is open and its media is up" % B.AGENT_NAME)

        # Other products bind their protocols to every new adapter, and a
        # foreign bridge protocol makes Windows refuse ours with "An
        # unexpected error occurred while configuring the Network Bridge".
        # Measured here: VMware's bridge protocol and two WireGuard
        # protocol drivers were on it. Strip it back to plain IPv4.
        stripped = B.prepare_adapter(B.AGENT_NAME)
        if stripped:
            log("  cleared %d protocol(s) off %s so it can be bridged: %s"
                % (len(stripped), B.AGENT_NAME, ", ".join(stripped)))

        # Note the plant address BEFORE anything changes. The bridge has
        # dropped to APIPA three times on this machine, and each time the
        # plant PC lost its own PLC until someone noticed.
        if not self._plant_ip:
            self._plant_ip = ip_pfx
            if self._plant_ip:
                log("  plant address noted: %s" % self._plant_ip)

        # Re-read only if the strip above changed anything; otherwise the
        # survey already told us.
        st = (B.bridge_state(B.AGENT_NAME) if stripped
              else {"bridge": info.get("bridge", ""),
                    "members": info.get("members", []),
                    "ready": info.get("ready", False)})
        if not st["ready"]:
            steps = B.setup_instructions(name, B.AGENT_NAME,
                                         existing_bridge=st["bridge"])
            log("BRIDGE NOT SET UP - %s is up but not a bridge member"
                % B.AGENT_NAME)
            for ln in steps:
                log("  " + ln)
            try:
                B.open_network_connections()
            except Exception:                               # noqa: BLE001
                pass
            sec.send(pack_ctrl({
                "op": "bridge_up", "error": "the plant PC is not bridged yet",
                "setup": steps, "plant_adapter": name,
                "adapter": B.AGENT_NAME,
                "bridge": st["bridge"], "members": st["members"]}))
            return

        # Keep the plant address alive for as long as the bridge exists.
        try:
            self._plant_guard = B.PlantWatchdog(plant_cidr, self._plant_ip,
                                                log=log)
            self._plant_guard.start()
            log("  watching the plant address - it goes straight back if "
                "the bridge drops it")
        except Exception:                                   # noqa: BLE001
            pass
        log("BRIDGE UP - %s is in bridge %s" % (B.AGENT_NAME, st["bridge"]))
        log("  the engineer is now on %s" % plant_cidr)
        sec.send(pack_ctrl({"op": "bridge_up", "ok": True,
                            "subnet": plant_cidr, "plant_adapter": name,
                            "plant_ip": ip_pfx,
                            "note": "%s is bridged into %s"
                                    % (B.AGENT_NAME, st["bridge"])}))

    def _bridge_down(self, sec: Secure) -> None:
        g = self._plant_guard
        if g:
            g.stop()
            if g.repairs:
                log("the plant address had to be put back %d time(s) while "
                    "the bridge was up" % g.repairs)
            self._plant_guard = None
        if not self.tunnel:
            sec.send(pack_ctrl({"op": "bridge_down", "ok": True}))
            return
        st = dict(self.tunnel.stats)
        self.tunnel.down()
        self.tunnel = None
        log("BRIDGE DOWN - %s" % st)
        sec.send(pack_ctrl({"op": "bridge_down", "ok": True, "stats": st}))

    def _tunnel_up(self, sec: Secure, msg: dict) -> None:
        """Serve the plant network through a userspace stack.

        The plant PC needs no adapter, no bridge, no NAT and no
        administrator rights: every connection the engineer makes is
        re-opened here as an ordinary socket, so the device answers this
        PC exactly as it always has.
        """
        import ipaddress
        import tnlink_netstack as NS
        if self.tunnel:
            sec.send(pack_ctrl({"op": "tunnel_up", "error":
                                "something is already up on this agent"}))
            return
        # Only the networks a device could actually live on. Offering
        # the WireGuard tunnel and VMware's private switches invited the
        # engineer to route ranges that mean nothing to them and that
        # their own machine is likely to be using.
        import tnlink_bridge as B
        lans = [a["cidr"] for a in local_subnets()
                if a.get("kind", "lan") == "lan"]
        allowed = {str(n) for n in self.allow}
        offered = [c for c in lans if c in allowed] or [
            c for c in lans] or [str(n) for n in self.allow]
        want = str(msg.get("subnet") or "").strip()
        if want:
            offered = [c for c in offered if c == want] or offered
        if not offered:
            sec.send(pack_ctrl({"op": "tunnel_up", "error":
                                "this agent has no ordinary network to "
                                "share"}))
            return
        nets = [ipaddress.ip_network(n, strict=False) for n in offered]
        log("  sharing only: %s" % ", ".join(offered))
        stack = NS.NetStack(lambda p: sec.send(pack_tun(p)), nets, log=log)
        self.tunnel = stack
        log("NETWORK SHARED - serving %s through the userspace stack"
            % ", ".join(offered))
        log("  no adapter, no bridge and no NAT on this PC")
        sec.send(pack_ctrl({
            "op": "tunnel_up", "ok": True, "mode": "netstack",
            "subnets": offered, "client_ip": "10.77.0.2",
            "router_ip": "10.77.0.1", "mtu": 1380,
            "note": "devices will answer this PC, so nothing on the "
                    "plant network changes"}))

    def _tunnel_down(self, sec: Secure) -> None:
        if not self.tunnel:
            sec.send(pack_ctrl({"op": "tunnel_down", "ok": True}))
            return
        t = self.tunnel
        self.tunnel = None
        summary = ""
        try:
            if hasattr(t, "summary"):
                summary = t.summary()
            if hasattr(t, "stop"):
                t.stop()
            elif hasattr(t, "down"):
                t.down()
        except Exception:                                   # noqa: BLE001
            pass
        log("NETWORK RELEASED - %s" % (summary or "closed"))
        sec.send(pack_ctrl({"op": "tunnel_down", "ok": True,
                            "summary": summary}))

    def _scan(self, sec: Secure, msg: dict) -> None:
        cidr = str(msg.get("cidr") or "")
        ports = [int(p) for p in (msg.get("ports") or [])][:6]
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError as exc:
            sec.send(pack_ctrl({"op": "scan", "error": str(exc)}))
            return
        if net.num_addresses > 256:
            sec.send(pack_ctrl({"op": "scan",
                                "error": "refusing more than a /24 at once"}))
            return
        hosts = [str(h) for h in net.hosts()]
        allowed = [h for h in hosts if self.permitted(h)[0]]
        if not allowed:
            sec.send(pack_ctrl({"op": "scan",
                                "error": "%s is outside the allowlist" % cidr}))
            return
        log("scan %s (%d host(s), ports=%s)" % (cidr, len(allowed), ports))

        found = []
        lock = threading.Lock()
        work = queue.Queue()
        for h in allowed:
            work.put(h)

        def worker():
            while True:
                try:
                    ip = work.get_nowait()
                except queue.Empty:
                    return
                r = icmp_once(ip, 350)
                entry = None
                if r.get("ok"):
                    entry = {"ip": ip, "rtt_ms": r["rtt_ms"], "ttl": r["ttl"],
                             "ports": []}
                elif ports:
                    entry = {"ip": ip, "rtt_ms": None, "ttl": None,
                             "ports": []}
                if entry is not None and ports:
                    for p in ports:
                        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        s.settimeout(0.35)
                        try:
                            s.connect((ip, p))
                            entry["ports"].append(p)
                        except OSError:
                            pass
                        finally:
                            s.close()
                if entry is not None and (entry["rtt_ms"] is not None
                                          or entry["ports"]):
                    with lock:
                        found.append(entry)
                work.task_done()

        # A /24 at 64 workers is four serial rounds of timeouts. The work
        # is pure waiting, so more threads cost almost nothing and turn
        # half a minute into a few seconds.
        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(min(192, len(allowed)))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        found.sort(key=lambda e: ipaddress.ip_address(e["ip"]))

        # Ask each device what it IS, from HERE. 2026-10-29, requested:
        # "retrieve information about supplier and part number, give us an
        # idea what type of device or module was found" - a port number
        # says EtherNet/IP, which is every controller, drive and I/O block
        # on the floor.
        #
        # It runs on the PLANT, beside the devices, where a probe costs a
        # millisecond. Asking from the engineer's end would cost a full
        # round trip to the relay per device - about 80 ms measured, so
        # twenty devices would be most of a minute of waiting.
        try:
            import tnlink_identify as ID

            def _id(entry):
                try:
                    info = ID.identify(ID.plain_tcp, entry["ip"],
                                       entry.get("ports"))
                    if info:
                        entry.update(info)
                        entry["what"] = ID.describe(info)
                except Exception:                         # noqa: BLE001
                    pass

            ids = [threading.Thread(target=_id, args=(e,), daemon=True)
                   for e in found]
            for t in ids:
                t.start()
            for t in ids:
                t.join(timeout=8)
        except Exception as exc:                          # noqa: BLE001
            log("identify skipped: %s" % exc)

        log("scan %s -> %d device(s)" % (cidr, len(found)))
        sec.send(pack_ctrl({"op": "scan", "cidr": cidr, "found": found}))

    def _open(self, sec: Secure, msg: dict) -> None:
        sid = int(msg.get("sid") or 0)
        ip = str(msg.get("ip") or "")
        port = int(msg.get("port") or 0)
        ok, why = self.permitted(ip)
        if not ok:
            log("open %s:%d REFUSED - %s" % (ip, port, why))
            sec.send(pack_ctrl({"op": "open", "sid": sid, "error": why}))
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(8.0)
        try:
            s.connect((ip, port))
        except OSError as exc:
            s.close()
            log("open %s:%d failed - %s" % (ip, port, exc.strerror or exc))
            sec.send(pack_ctrl({"op": "open", "sid": sid,
                                "error": str(exc.strerror or exc)}))
            return
        s.settimeout(None)
        with self.lock:
            self.streams[sid] = s
        log("open %s:%d -> stream %d" % (ip, port, sid))
        sec.send(pack_ctrl({"op": "open", "sid": sid, "ok": True}))
        threading.Thread(target=self._pump, args=(sec, sid, s),
                         daemon=True).start()

    def _pump(self, sec: Secure, sid: int, s: socket.socket) -> None:
        try:
            while True:
                chunk = s.recv(32768)
                if not chunk:
                    break
                sec.send(pack_data(sid, chunk))
        except Exception:
            pass
        finally:
            self._close_stream(sec, sid, "remote closed")

    def _close_stream(self, sec: Secure, sid: int, why: str,
                      tell: bool = True) -> None:
        with self.lock:
            s = self.streams.pop(sid, None)
        if s:
            try:
                s.close()
            except Exception:
                pass
            log("stream %d closed (%s)" % (sid, why))
            if tell:
                try:
                    sec.send(pack_ctrl({"op": "close", "sid": sid,
                                        "reason": why}))
                except Exception:
                    pass


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------
class Client:
    def __init__(self, host: str, port: int, code: str):
        self.host, self.port, self.code = host, port, code
        self.sec = None
        self.info = {}
        self.replies = queue.Queue()
        self.streams = {}
        self.maps = []
        self.devices = []
        self.claimed = []
        #: Devices routed at their REAL address over the tunnel. `claimed`
        #: and `maps` already covered the other two ways in; this one was
        #: not recorded anywhere, so the interface's Connected list stayed
        #: empty after option 1 had plainly worked.
        self.routed = []
        #: Measured cost of one exchange with the plant, in milliseconds,
        #: and the networks both ends share. Both are filled in at connect.
        self.link_ms = 0.0
        self.same_lan = []
        self.tunnel = None
        self.tunnel_info = {}
        self.relay = ""
        self.next_sid = 1
        self.lock = threading.Lock()
        self.alive = True

    def __init_relay__(self, relay: str) -> None:
        self.relay = relay

    def connect(self) -> None:
        relay = getattr(self, "relay", "")
        if relay:
            s = relay_dial(relay, self.code, is_agent=False)
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(15.0)
            s.connect((self.host, self.port))
            s.settimeout(None)
        self.sec = handshake(s, self.code, is_server=False)
        threading.Thread(target=self._reader, daemon=True).start()
        self.sec.send(pack_ctrl({"op": "hello"}))
        self.info = self._await("hello", 15) or {}
        # 2026-10-31: file this machine in the address book the moment we know
        # who it is. Keyed by its permanent unit id, so the entry survives the
        # pairing code rotating and the far end being renamed - which is what
        # "always be able to reconnect" actually needs.
        try:
            if self.info.get("unit") and self.info.get("next_code"):
                # File the next code against the SERVER's permanent unit id,
                # so it survives that machine being renamed and is not tied
                # to an address that may change.
                remember_next_code(str(self.info.get("unit") or ""),
                                   str(self.info.get("next_code") or ""))
        except Exception:
            pass
        try:
            if self.info.get("unit"):
                # The address book row FIRST: remember_trust only updates an
                # existing entry, so on a first pairing - the one that
                # actually hands out the key - doing it the other way round
                # dropped the key silently and the next connection asked for
                # a code again. Order matters here.
                remember_connection(str(self.info.get("unit") or ""),
                                    host=str(self.info.get("host") or ""),
                                    address="%s:%s" % (self.host, self.port),
                                    role="client")
                if self.info.get("trust"):
                    # Keep the per-pair key the server just handed us. From
                    # now on this machine reconnects without a code.
                    remember_trust(str(self.info.get("unit") or ""),
                                   str(self.info.get("trust") or ""))
        except Exception:
            pass                       # an address book must never break a link
        self.measure_link()
        self.detect_same_lan()
        self._announce_peer()

    def measure_link(self, samples: int = 3) -> float:
        """How long one exchange with the plant takes, end to end.

        Measured 2026-10-29: an exchange costs the same whatever its size -
        78.0 ms for 64 bytes and 78.0 ms for 4,002 - so this ONE number
        predicts everything an engineer cares about. A PLC download is tens
        of thousands of exchanges, and the time it will take is this
        multiplied by how many. Worth showing before someone starts a
        download and watches it for half an hour.

        `hello` is used as the probe because the agent already answers it
        and it changes nothing at the other end.
        """
        times = []
        for _ in range(max(1, samples)):
            try:
                t0 = time.perf_counter()
                self.sec.send(pack_ctrl({"op": "hello"}))
                if self._await("hello", 10) is None:
                    continue
                times.append((time.perf_counter() - t0) * 1000.0)
            except Exception:                               # noqa: BLE001
                break
        if times:
            times.sort()
            self.link_ms = times[len(times) // 2]
        return self.link_ms

    def detect_same_lan(self) -> list:
        """Networks this PC shares with the plant.

        If the two machines sit on the same network - one building, one
        Wi-Fi - then every exchange is making a round trip out to the relay
        and back for a machine that is metres away. Detecting it is the
        first half of doing something about it.

        The agent already sends its own subnets in `hello`, so this costs
        nothing on the wire.
        """
        self.same_lan = []
        try:
            import tnlink_tun as TT
            mine = [ipaddress.ip_network(c) for c in TT.local_prefixes()]
            for a in (self.info.get("subnets") or []):
                try:
                    net = ipaddress.ip_network(a.get("cidr"), strict=False)
                except (ValueError, TypeError):
                    continue
                for m in mine:
                    if net.overlaps(m) and str(net) not in self.same_lan:
                        self.same_lan.append(str(net))
        except Exception:                                   # noqa: BLE001
            self.same_lan = []
        return self.same_lan

    def _announce_peer(self) -> None:
        """Say WHICH machine answered, before anything else.

        2026-10-22, reported: an engineer paired, worked for forty minutes,
        and only then realised they were on YESTERDAY'S computer - a server
        left running there still owned that code's room on the relay, and
        re-armed itself after every session. The client knew the answer all
        along (the agent sends its hostname and networks in `hello`) but only
        printed it if you happened to type `status`.

        A connection code says nothing about which machine it reaches. Say it
        out loud, every time, so a wrong one is obvious in the first second.
        """
        host = str(self.info.get("host") or "").strip() or "unknown"
        nets = [str(n) for n in (self.info.get("allow") or [])]
        mins = int(self.info.get("expires_in_s") or 0) // 60
        print()
        print("  CONNECTED TO : %s" % host)
        print("  its networks : %s" % (", ".join(nets) or "none offered"))
        if self.link_ms:
            print("  link         : %.0f ms per exchange%s"
                  % (self.link_ms, " (through the relay)" if self.relay else ""))
        if self.same_lan:
            print("  NOTE         : this PC is on the same network as the")
            print("                 plant (%s) - every exchange is going out"
                  % ", ".join(self.same_lan))
            print("                 to the relay and back for a machine that")
            print("                 is right here.")
        if mins > 0:
            print("  code expires : in %d minute(s)" % mins)
        print("  If that is not the machine you expected, type 0 and check "
              "the code.")
        print()

    def _reader(self) -> None:
        try:
            while self.alive:
                kind, a, b = parse(self.sec.recv())
                if kind == TUN:
                    if self.tunnel:
                        self.tunnel.inject(b)
                    continue
                if kind == DATA:
                    with self.lock:
                        s = self.streams.get(a)
                    if s:
                        try:
                            s.sendall(b)
                        except OSError:
                            pass
                    continue
                if a.get("op") == "close":
                    with self.lock:
                        s = self.streams.pop(a.get("sid"), None)
                    if s:
                        try:
                            s.close()
                        except Exception:
                            pass
                    continue
                if a.get("op") == "bye":
                    print("\n  server closed the link: %s"
                          % a.get("reason", "?"))
                    self.alive = False
                    return
                self.replies.put(a)
        except Exception as exc:
            if self.alive:
                print("\n  link lost: %s" % type(exc).__name__)
            self.alive = False

    def _await(self, op: str, timeout: float = 30.0):
        end = time.time() + timeout
        while time.time() < end:
            try:
                msg = self.replies.get(timeout=max(0.1, end - time.time()))
            except queue.Empty:
                break
            if msg.get("op") == op:
                return msg
        return None

    # -- commands ---------------------------------------------------------
    def cmd_info(self) -> None:
        i = self.info
        print("  server      : %s (%s)" % (i.get("host", "?"),
                                           i.get("version", "?")))
        subs = i.get("subnets") or []
        # A bare list of five addresses is what sent the operator to a
        # VMware host-only adapter in the first place. Name the adapter and
        # put the networks a PLC actually lives on at the top.
        for label, want in (("network", "lan"), ("vpn", "vpn"),
                            ("local-only", "local")):
            for s in subs:
                if s.get("kind", "lan") != want:
                    continue
                print("  %-11s : %-15s %-30s %s"
                      % (label, s["cidr"], str(s.get("desc") or "")[:30],
                         "(has a gateway)" if s.get("gateway") else ""))
        print("  reachable   : %s" % ", ".join(i.get("allow") or []))
        exp = int(i.get("expires_in_s") or 0)
        if exp:
            print("  expires in  : %d min" % round(exp / 60))

    def cmd_ping(self, ip: str, count: int = 4) -> None:
        self.sec.send(pack_ctrl({"op": "ping", "ip": ip, "count": count}))
        r = self._await("ping", 10 + count * 2)
        if not r:
            print("  no answer from the server")
            return
        if r.get("error"):
            print("  refused: %s" % r["error"])
            return
        for rep in r.get("replies") or []:
            if rep.get("ok"):
                print("  reply from %s: time=%dms TTL=%d"
                      % (r["ip"], rep["rtt_ms"], rep["ttl"]))
            else:
                print("  %s: %s" % (r["ip"], rep.get("reason", "no reply")))
        lost = r["sent"] - r["received"]
        print("  %d sent, %d received, %d lost (%.0f%% loss)%s"
              % (r["sent"], r["received"], lost,
                 100.0 * lost / max(1, r["sent"]),
                 "  min/avg/max = %d/%s/%d ms"
                 % (r["rtt_min"], r["rtt_avg"], r["rtt_max"])
                 if r.get("received") else ""))

    def cmd_scan(self, cidr: str, ports: list) -> None:
        self.sec.send(pack_ctrl({"op": "scan", "cidr": cidr, "ports": ports}))
        print("  scanning %s%s ..."
              % (cidr, (" ports %s" % ports) if ports else ""))
        r = self._await("scan", 120)
        if not r:
            print("  no answer from the server")
            return
        if r.get("error"):
            print("  %s" % r["error"])
            return
        found = r.get("found") or []
        if not found:
            print("  nothing answered in %s" % cidr)
            return
        print("  %-16s %8s %5s  %s" % ("address", "rtt", "ttl", "open ports"))
        for e in found:
            print("  %-16s %8s %5s  %s"
                  % (e["ip"],
                     "%d ms" % e["rtt_ms"] if e["rtt_ms"] is not None else "-",
                     e["ttl"] if e["ttl"] is not None else "-",
                     ", ".join(str(p) for p in e["ports"]) or "-"))
        print("  %d device(s)" % len(found))

    def cmd_map(self, local: str, remote: str, quiet: bool = False) -> None:
        if ":" in local:
            lhost, lport = local.rsplit(":", 1)
        else:
            lhost, lport = "127.0.0.1", local
        try:
            lport = int(lport)
            rip, rport = remote.rsplit(":", 1)
            rport = int(rport)
        except ValueError:
            print("  usage: map [bind:]localport remoteip:remoteport")
            return
        try:
            ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ls.bind((lhost, lport))
            ls.listen(8)
        except OSError as exc:
            print("  cannot listen on %s:%d - %s"
                  % (lhost, lport, exc.strerror or exc))
            if lport == 3389:
                print("  Your own PC runs Remote Desktop on 3389, so it")
                print("  cannot also be the near end of a forward.")
                print("  Use the rdp command - it picks a free port.")
            else:
                print("  Something else already uses that local port.")
                print("  Pick another, e.g. %d." % (lport + 10000))
            return
        self.maps.append({"local": "%s:%d" % (lhost, lport),
                          "remote": "%s:%d" % (rip, rport)})
        threading.Thread(target=self._acceptor, args=(ls, rip, rport),
                         daemon=True).start()
        if not quiet:
            print("  %s:%d  ->  %s:%d" % (lhost, lport, rip, rport))
            print("  point your program at %s:%d" % (lhost, lport))
            self._warn_if_local(rip, lhost, lport)

    def _acceptor(self, ls: socket.socket, rip: str, rport: int) -> None:
        while self.alive:
            try:
                conn, addr = ls.accept()
            except OSError:
                return
            with self.lock:
                sid = self.next_sid
                self.next_sid += 1
            self.sec.send(pack_ctrl({"op": "open", "sid": sid,
                                     "ip": rip, "port": rport}))
            r = self._await("open", 15)
            if not r or not r.get("ok"):
                print("\n  %s:%d refused: %s"
                      % (rip, rport, (r or {}).get("error", "no answer")))
                conn.close()
                continue
            with self.lock:
                self.streams[sid] = conn
            threading.Thread(target=self._pump, args=(sid, conn),
                             daemon=True).start()

    def _pump(self, sid: int, conn: socket.socket) -> None:
        try:
            while self.alive:
                chunk = conn.recv(32768)
                if not chunk:
                    break
                self.sec.send(pack_data(sid, chunk))
        except Exception:
            pass
        finally:
            with self.lock:
                self.streams.pop(sid, None)
            try:
                self.sec.send(pack_ctrl({"op": "close", "sid": sid}))
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    # -- console ----------------------------------------------------------
    # -- helpers ----------------------------------------------------------
    def _server_lans(self) -> list:
        return [a for a in (self.info.get("subnets") or [])
                if a.get("kind", "lan") == "lan"]

    def _server_ip(self) -> str:
        """The agent PC's own best address - what RDP should target."""
        lans = self._server_lans()
        for a in lans:
            if a.get("gateway"):
                return a.get("ip") or ""
        return (lans[0].get("ip") if lans else "") or ""

    @staticmethod
    def _free_port(preferred: int) -> int:
        """A local port we can actually bind.

        3389 is the trap: the engineer's own PC almost always runs Remote
        Desktop, so binding 3389 locally fails and the forward silently
        never happens. Measured on the reporter's PC, TermService already
        held 0.0.0.0:3389 - which is why plain `map 3389` did nothing.
        """
        for cand in [preferred] + list(range(preferred + 1, preferred + 40)):
            t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                t.bind(("127.0.0.1", cand))
                return cand
            except OSError:
                continue
            finally:
                t.close()
        return 0

    def _resolve(self, token: str) -> str:
        """Accept an IP, or the number from the last devices listing."""
        token = (token or "").strip()
        if token.isdigit() and self.devices:
            n = int(token)
            if 1 <= n <= len(self.devices):
                return self.devices[n - 1]["ip"]
            print("  there is no device %d - run devices again" % n)
            return ""
        return token

    # -- commands ---------------------------------------------------------
    def cmd_devices(self, cidr: str = "", ports: str = "") -> None:
        """Scan and remember, so later commands can say 3 instead of an IP."""
        targets = [cidr] if cidr else [a["cidr"] for a in self._server_lans()]
        if not targets:
            print("  the server reported no ordinary network to scan")
            return
        if ports:
            plist = [int(x) for x in ports.split(",") if x.strip().isdigit()]
        else:
            plist = [44818, 80, 443, 502, 3389]
        found = []
        for t in targets:
            self.sec.send(pack_ctrl({"op": "scan", "cidr": t,
                                     "ports": plist}))
            print("  scanning %s for ports %s ..."
                  % (t, ", ".join(str(x) for x in plist)))
            r = self._await("scan", 180)
            if not r:
                print("    no answer")
                continue
            if r.get("error"):
                print("    %s" % r["error"])
                continue
            found.extend(r.get("found") or [])
        self.devices = found
        if not found:
            print("  nothing answered")
            return
        print()
        print("  %-3s %-16s %7s  %s" % ("#", "address", "rtt", "open ports"))
        for n, e in enumerate(found, 1):
            got = e.get("ports") or []
            hint = ""
            if 44818 in got:
                hint = "  <- EtherNet/IP (Logix)"
            elif 502 in got:
                hint = "  <- Modbus TCP"
            elif 3389 in got:
                hint = "  <- Remote Desktop"
            print("  %-3d %-16s %7s  %s%s"
                  % (n, e["ip"],
                     ("%d ms" % e["rtt_ms"]) if e["rtt_ms"] is not None
                     else "-",
                     ", ".join(str(x) for x in got) or "-", hint))
        print()
        print("  now:  map 44818 1:44818    forward device 1 port 44818")
        print("        rdp 1                Remote Desktop to device 1")
        print("        claim 1              device 1 at its REAL address")

    def _warn_if_local(self, rip: str, lhost: str, lport: int) -> None:
        """The address exists on THIS machine's own network - a trap.

        If this PC (or VM) is itself on the device's subnet, Windows treats
        the device's address as local: it ARPs for it on the local segment
        and never routes it through anything. Software configured with the
        real address can then never reach the tunnel, however healthy the
        session is. Found inside a VM whose VMware subnet happened to be
        the same 192.168.10.0/24 as the plant network.
        """
        try:
            target = ipaddress.ip_address(rip)
        except ValueError:
            return
        for a in local_subnets():
            try:
                net = ipaddress.ip_network(a["cidr"], strict=False)
            except ValueError:
                continue
            if target not in net:
                continue
            print()
            print("  NOTE this PC is ITSELF on %s (%s)."
                  % (a["cidr"], str(a.get("desc") or "")[:30]))
            print("  So Windows treats %s as a LOCAL address: it will" % rip)
            print("  look for it on this network and never use the tunnel.")
            print("  Configure your software for %s:%d - NOT %s."
                  % (lhost, lport, rip))
            return

    def cmd_bridge(self, arg: str = "") -> None:
        """Join the plant network itself - the real thing.

        A TN-Link adapter appears on this PC and on the plant PC, the
        plant PC bridges its end to the network the PLC is on, and this PC
        becomes a member of that segment. The PLC keeps its address, this
        PC just gets one on the same range, and PLC software sees
        everything exactly as it would on site.
        """
        import tnlink_bridge as B
        if arg.lower() in ("down", "off", "stop"):
            self.cmd_bridge_down()
            return
        if self.tunnel:
            print("  already up - type  bridge down  first")
            return
        if not _is_admin():
            print("  the TN-Link adapter needs administrator rights.")
            print("  Close this and re-run tnlink.exe with")
            print("  right-click > Run as administrator.")
            return

        lans = [a for a in (self.info.get("subnets") or [])
                if a.get("kind", "lan") == "lan"]
        if not lans:
            print("  the plant PC reported no ordinary network to share")
            return
        if len(lans) > 1 and not arg:
            print("  which network should be shared?")
            for a in lans:
                print("    bridge %-18s (%s)"
                      % (a["cidr"], str(a.get("desc") or "")[:30]))
            return
        want = arg or lans[0]["cidr"]

        print("  sharing %s ..." % want, flush=True)
        self.sec.send(pack_ctrl({"op": "bridge_up", "subnet": want}))
        r = self._await("bridge_up", 180)
        if not r:
            print("  the plant PC did not answer")
            return
        if r.get("error"):
            print("  %s" % r["error"])
            steps = r.get("setup") or []
            if steps:
                print()
                print("  ON THE PLANT PC, one time only:")
                print("  " + "-" * 60)
                for ln in steps:
                    print("  " + ln)
                print("  " + "-" * 60)
                print("  Network Connections has been opened there for you.")
                print("  Then run  bridge  again.")
            return
        print("  plant side: %s" % r.get("note"))

        try:
            ep = B.L2Endpoint(lambda f: self.sec.send(pack_tun(f)),
                              name=B.CLIENT_NAME, keep_adapter=False,
                              filter_flood=False)
            ep.up()
        except Exception as exc:                            # noqa: BLE001
            print("  could not create the TN-Link adapter here: %s" % exc)
            self.sec.send(pack_ctrl({"op": "bridge_down"}))
            self._await("bridge_down", 60)
            return
        self.tunnel = ep
        self.tunnel_info = r

        print()
        print("  TN-Link is UP - this PC is now on %s" % r.get("subnet"))

    def cmd_bridge_ip(self, spec: str) -> None:
        if not self.tunnel:
            print("  bring the bridge up first:  bridge")
            return
        if spec.lower() == "dhcp":
            ok, why = self.tunnel.use_dhcp()
            if ok:
                print("  got %s" % why)
            else:
                print("  %s" % why)
            return
        if "/" not in spec:
            print("  usage: bridge ip 192.168.10.77/24")
            return
        ip, _, pfx = spec.partition("/")
        try:
            prefix = int(pfx)
        except ValueError:
            print("  %r is not a prefix length" % pfx)
            return
        ok, why = self.tunnel.set_ip(ip.strip(), prefix)
        print("  %s/%d %s%s" % (ip.strip(), prefix,
                                "set" if ok else "FAILED",
                                "" if ok else " - " + why[:90]))
        if ok:
            print("  try:  ping <the PLC>   from a normal command prompt")

    def cmd_bridge_down(self) -> None:
        if not self.tunnel:
            print("  nothing is up")
            return
        st = dict(self.tunnel.stats)
        self.tunnel.down()
        self.tunnel = None
        try:
            self.sec.send(pack_ctrl({"op": "bridge_down"}))
            self._await("bridge_down", 120)
        except Exception:                                   # noqa: BLE001
            pass
        print("  TN-Link removed at both ends (%d frames out, %d in)"
              % (st.get("out", 0), st.get("in", 0)))

    def cmd_tunnel(self, arg: str = "") -> None:
        """Bring up a TN-Link adapter at both ends, and route the plant
        network over it.

        After this the PLC is reachable at its OWN address - RSLinx and
        Studio 5000 keep the controller's real IP and need no special
        configuration at all.
        """
        import tnlink_tun as TT
        if arg.lower() in ("down", "off", "stop"):
            self.cmd_tunnel_down()
            return
        if self.tunnel:
            print("  the tunnel is already up - type  tunnel down  first")
            return
        if not _is_admin():
            print("  the TN-Link adapter needs administrator rights.")
            print("  Close this and re-run tnlink.exe with")
            print("  right-click > Run as administrator.")
            return

        print("  asking the agent to bring its end up ...", flush=True)
        self.sec.send(pack_ctrl({"op": "tunnel_up"}))
        r = self._await("tunnel_up", 120)
        if not r:
            print("  the agent did not answer")
            return
        if r.get("error"):
            print("  the agent could not start its end: %s" % r["error"])
            return
        subnets = [c for c in (r.get("subnets") or [])
                   if c != r.get("tunnel_cidr")]
        print("  plant end ready: %s"
              % (r.get("note") or r.get("mode") or "ok"))
        for n in r.get("notes") or []:
            print("    %s" % n)

        # Refuse a subnet this machine is already on - routing it would
        # break this PC's own networking, which is exactly the trap a VM
        # on 192.168.10.x falls into.
        host_routes = []
        bad = TT.conflicts(subnets)
        if bad:
            # Being on the same range is a real clash, but it is the
            # operator's network and their call. Offer the ones that are
            # free rather than refusing everything, and let them override
            # if the clash is the one they actually want.
            clashing = {r for r, _ in bad}
            free = [c for c in subnets if c not in clashing]
            print()
            for remote, mine in bad:
                print("  %s clashes - this PC is already on %s"
                      % (remote, mine))
            if free:
                print()
                print("  Sharing the ones that do not clash: %s"
                      % ", ".join(free))
                subnets = free
            else:
                # 2026-10-28. This used to end here, telling the operator to
                # "move this PC (or the VM) off that range" - which is their
                # home router, so it is advice nobody can take.
                #
                # Measured instead: a /32 host route OVERRIDES the on-link
                # /24 and the internet keeps working (192.168.0.222 moved to
                # the tunnel adapter while 8.8.8.8 stayed on WiFi). So the
                # whole subnet cannot come over - but the DEVICES on it can,
                # at their REAL addresses, which is what RSLinx and the PLC
                # project need.
                print()
                print("  Every offered network clashes with one this PC is")
                print("  already on, so the whole range cannot be routed")
                print("  here - but single devices still can, keeping their")
                print("  REAL addresses.")
                print()
                print("  Type the device addresses to reach, comma separated")
                print("  (e.g. the PLC), or press Enter to stop:")
                raw = input("  devices> ").strip()
                wanted = [x.strip() for x in raw.split(",") if x.strip()]
                if not wanted:
                    self.sec.send(pack_ctrl({"op": "tunnel_down"}))
                    self._await("tunnel_down", 60)
                    return
                blocked = []
                usable = []
                for ip in wanted:
                    why = TT.address_is_in_use_locally(ip)
                    if why:
                        blocked.append((ip, why))
                    else:
                        usable.append(ip)
                for ip, why in blocked:
                    print()
                    print("  %s cannot be routed here: %s" % (ip, why))
                    print("  One address cannot be two machines. Reach that")
                    print("  device from a PC that is not on this range, or")
                    print("  use option 4 to forward its ports instead.")
                if not usable:
                    self.sec.send(pack_ctrl({"op": "tunnel_down"}))
                    self._await("tunnel_down", 60)
                    return
                subnets = []
                host_routes = usable

        try:
            ep = TT.Endpoint(r.get("client_ip") or TT.CLIENT_IP,
                             lambda pkt: self.sec.send(pack_tun(pkt)))
            ep.up()
        except Exception as exc:                            # noqa: BLE001
            print("  could not create the TN-Link adapter: %s" % exc)
            self.sec.send(pack_ctrl({"op": "tunnel_down"}))
            self._await("tunnel_down", 60)
            return
        done, failed = ep.add_routes(subnets)
        # Single devices by their real address - see the clash branch above.
        if host_routes:
            hdone, hfailed = ep.add_host_routes(host_routes)
            done = list(done) + ["%s (device)" % h for h in hdone]
            failed = list(failed) + hfailed
        self.tunnel = ep
        self.tunnel_info = r

        print()
        print("  THE PLANT NETWORK IS NOW SHARED WITH THIS PC")
        print("  " + "-" * 60)
        print("    adapter   : TN-Link  (%s, MTU %d)" % (ep.ip, TT.MTU))
        for c in done:
            print("    reaching  : %s  - every device, at its OWN address"
                  % c)
        for f in failed:
            print("    NOT routed: %s" % f)
        print("  " + "-" * 60)
        print()
        print("  Try it from a normal command prompt:")
        print("      ping 192.168.10.240")
        print()
        print("  RSLinx: Communications > Configure Drivers >")
        print("          Ethernet devices, Station 0 = the PLC's own IP")
        print("          (not the EtherNet/IP driver - it browses by")
        print("           broadcast, which no tunnel can carry)")
        print()
        print("  Nothing was changed on the plant network: the plant PC")
        print("  opens each connection itself, so devices answer it the")
        print("  way they always have.")
        print()
        print("  Type  6  again or  tunnel down  to remove it.")

    def cmd_tunnel_down(self) -> None:
        if not self.tunnel:
            print("  no tunnel is up")
            return
        st = dict(self.tunnel.stats)
        self.tunnel.down()
        self.tunnel = None
        try:
            self.sec.send(pack_ctrl({"op": "tunnel_down"}))
            self._await("tunnel_down", 60)
        except Exception:                                   # noqa: BLE001
            pass
        print("  TN-Link removed at both ends "
              "(%d packets out, %d in)" % (st.get("out", 0), st.get("in", 0)))

    def _plc_real_address(self, ip: str) -> bool:
        """Route this ONE device over the tunnel, keeping its real address.

        Returns True when the engineer can now use `ip` itself. False means
        the caller should fall back to the loopback forward - which is
        still a working answer, just not the address the project expects.

        Measured 2026-10-28 on a client whose WiFi is 192.168.0.161/24: a
        /32 for 192.168.0.222 moved ONLY that address onto the tunnel
        adapter and 8.8.8.8 still went out over WiFi. The exception is an
        address this PC already needs - its own gateway, or one it holds -
        and that is refused with the reason rather than attempted.
        """
        # `import tnlink_tun as TT` lives inside cmd_tunnel, so it was not
        # in scope here and this raised "name 'TT' is not defined" the
        # moment the adapter was needed - reported from a live test.
        import tnlink_tun as TT
        why = ""
        try:
            why = TT.address_is_in_use_locally(ip)
        except Exception:
            why = ""
        if why:
            print()
            print("  %s cannot be used as-is here: %s" % (ip, why))
            print("  Falling back to a local forward, so RSLinx will see")
            print("  127.0.0.1 instead of the real address.")
            return False
        if self.tunnel is None:
            self.sec.send(pack_ctrl({"op": "tunnel_up"}))
            r = self._await("tunnel_up", 120) or {}
            if not r.get("ok"):
                print("  the plant end could not share its network: %s"
                      % (r.get("error") or "no answer"))
                return False
            try:
                ep = TT.Endpoint(r.get("client_ip") or TT.CLIENT_IP,
                                 lambda pkt: self.sec.send(pack_tun(pkt)))
                ep.up()
            except Exception as exc:                     # noqa: BLE001
                # 2026-10-28, seen in a live test: "the TN-Link adapter
                # needs administrator rights". The real address depends on
                # an adapter, the adapter depends on elevation, and without
                # it this quietly dropped back to 127.0.0.1 - which is why
                # the operator kept seeing the loopback address and could
                # not tell why. Say what to do about it.
                msg = str(exc)
                print("  could not create the TN-Link adapter: %s" % msg)
                if "administrator" in msg.lower():
                    print()
                    print("  The REAL address needs the adapter, and the")
                    print("  adapter needs administrator rights. Close this")
                    print("  and start tnlink.exe with:")
                    print()
                    print("      right-click > Run as administrator")
                    print()
                    print("  Falling back to a local forward for now, so")
                    print("  RSLinx will see 127.0.0.1 instead of %s." % ip)
                return False
            self.tunnel = ep
            self.tunnel_info = r
        done, failed = self.tunnel.add_host_routes([ip])
        if not done:
            print("  could not route %s here: %s"
                  % (ip, "; ".join(failed) or "unknown"))
            return False
        if ip not in self.routed:
            self.routed.append(ip)
        print()
        print("  %s is now reachable AT ITS REAL ADDRESS from this PC." % ip)
        print()
        print("  In RSLinx Classic:")
        print("    Communications > Configure Drivers")
        print("    Add New > Ethernet devices")
        print("    Station 0:  %s" % ip)
        print()
        print("  That is the address in the PLC project, so the project")
        print("  path does not change and nothing has to be re-pointed.")
        print()
        ok = False
        try:
            import socket as _sk, struct as _st
            _c = _sk.socket(); _c.settimeout(10)
            _c.connect((ip, 44818))
            _c.sendall(_st.pack("<HHIIQI", 0x63, 0, 0, 0, 0, 0))
            _r = _c.recv(1024)
            _c.close()
            if len(_r) >= 28 and _r[0] == 0x63 and _r[1] == 0:
                _txt = bytes(ch for ch in _r[40:] if 32 <= ch < 127)
                print("  VERIFIED - the controller answered on %s: %s"
                      % (ip, _txt.decode("ascii", "replace").strip()[:46]))
                ok = True
            else:
                print("  %s is routed but did not identify itself." % ip)
        except Exception as _exc:                        # noqa: BLE001
            print("  routed, but no CIP answer yet (%s) - try RSLinx anyway."
                  % type(_exc).__name__)
        return True

    def _pick_controller(self) -> str:
        """Scan, LIST what was found, and let the operator choose.

        2026-10-28, asked while testing: "I would like to understand if the
        PLC is being picked automatically, I think would be good have the
        networks scanned then option so the user can pick the plc or other
        devices". It did auto-pick whatever answered first, which is fine
        with one controller on the bench and wrong on a real line with
        several.
        """
        lans = [a["cidr"] for a in self._server_lans()]
        if not lans:
            print("  the server reported no ordinary network to scan")
            return ""
        found = []
        for cidr in lans:
            print("  scanning %s for devices ..." % cidr)
            self.sec.send(pack_ctrl({"op": "scan", "cidr": cidr,
                                     "ports": [44818, 502, 102, 80, 443]}))
            r = self._await("scan", 240) or {}
            for d in (r.get("found") or []):
                if d.get("ip") not in [f["ip"] for f in found]:
                    found.append(d)
        if not found:
            print("  nothing answered on %s" % ", ".join(lans))
            return ""
        # 2026-10-28, reported: "when I click option one it should give all
        # devices list so I can pick". It listed only when there was more
        # than one, so a single device was chosen silently and a second
        # device that was not a controller never appeared at all.
        print()
        print("  devices found:")
        for i, d in enumerate(found, 1):
            ports = d.get("ports") or []
            what = "controller" if 44818 in ports else "device"
            extra = ", ".join(str(x) for x in ports if x != 44818)
            print("    %d  %-16s %-11s %s%s"
                  % (i, d["ip"], what,
                     ("%d ms" % d["rtt_ms"]) if d.get("rtt_ms") is not None else "",
                     ("  ports %s" % extra) if extra else ""))
        print()
        while True:
            pick = input("  which one (1-%d, or Enter to cancel)> " % len(found)).strip()
            if not pick:
                return ""
            if pick.isdigit() and 1 <= int(pick) <= len(found):
                return found[int(pick) - 1]["ip"]
            print("  pick a number from the list.")

    def cmd_plc(self, who: str = "", port: str = "44818") -> None:
        """Reach a PLC - at its REAL address when that is possible.

        2026-10-28, reported while testing: "I noticed that the ip that is
        ping the plc is still the 127.0.0.1, I was expecting on this fix the
        real plc ip address".

        That was a fair complaint and the fix had gone to the wrong place:
        the /32 host-route work went into the SHARE-THE-NETWORK path, while
        this command has always been a loopback port forward
        (127.0.0.1:44818 -> plc:44818). A forward is easy and needs no
        adapter, but the address an engineer sees is not the PLC's, and the
        address matters: it is written into the project.

        So this now tries the real address first - the tunnel plus a /32
        host route, measured to override the on-link /24 while the internet
        keeps working - and falls back to the forward when that address is
        one this PC already needs.
        """
        if not who:
            who = self._pick_controller()
            if not who:
                return
        ip = self._resolve(who)
        if not ip:
            self.cmd_help("plc")
            return
        if self._plc_real_address(ip):
            return
        try:
            rport = int(port)
        except ValueError:
            rport = 44818
        lport = self._free_port(rport)
        if not lport:
            print("  no free local port")
            return
        self.cmd_map(str(lport), "%s:%d" % (ip, rport), quiet=True)
        print()
        print("  %s:%d  ->  %s:%d" % ("127.0.0.1", lport, ip, rport))
        print()
        print("  In RSLinx Classic:")
        print("    Communications > Configure Drivers")
        print("    Add New > Ethernet devices    (NOT EtherNet/IP driver -")
        print("                                   that one browses by")
        print("                                   broadcast and cannot")
        print("                                   cross a tunnel)")
        print("    Station 0:  127.0.0.1")
        if lport != 44818:
            print("    NOTE local port %d is in use here, so this forward" % rport)
            print("    is on %d. RSLinx cannot be told a port, so free" % lport)
            print("    %d and run `plc` again." % rport)
        print()
        print("  In FactoryTalk Linx:")
        print("    add an Ethernet driver, then add device 127.0.0.1")
        print()
        # Prove it, rather than asserting it. A forward that opens but
        # carries nothing looks identical to one that works.
        try:
            import socket as _sk, struct as _st
            _c = _sk.socket(); _c.settimeout(10)
            _c.connect(("127.0.0.1", lport))
            _c.sendall(_st.pack("<HHIIQI", 0x63, 0, 0, 0, 0, 0))
            _r = _c.recv(1024)
            _c.close()
            if len(_r) >= 28 and _r[0] == 0x63 and _r[1] == 0:
                _txt = bytes(ch for ch in _r[40:] if 32 <= ch < 127)
                _name = _txt.decode("ascii", "replace").strip()
                print("  VERIFIED - the controller answered through the")
                print("  forward: %s" % _name[:46])
            else:
                print("  the port is open but the controller did not")
                print("  identify itself - check it really is a PLC.")
        except Exception as _exc:
            print("  could not reach it through the forward yet (%s)."
                  % type(_exc).__name__)
            print("  It may just be slow - try RSLinx anyway.")
        print()
        print("  Then browse - the controller appears under that address.")

    # What an engineer actually needs to reach on a device, whatever it
    # is - a controller, an HMI, a drive, a managed switch, a camera.
    DEVICE_PORTS = (
        (44818, "EtherNet/IP  (RSLinx, Studio 5000)"),
        (502, "Modbus TCP"),
        (102, "S7 / ISO-TSAP"),
        (80, "web interface"),
        (443, "web interface (https)"),
        (8080, "web interface (alt)"),
        (22, "SSH"),
        (23, "Telnet"),
        (21, "FTP"),
        (3389, "Remote Desktop"),
        (5900, "VNC"),
        (2222, "EtherNet/IP (explicit)"),
    )

    def cmd_device(self, who: str = "", extra: str = "") -> None:
        """Forward a whole device, not just one port.

        The same tunnel carries anything, so there is no reason to make
        the operator work out which port a drive's web page is on.
        Whatever answers gets a local port; whatever does not is simply
        not listening, and is reported as such rather than silently
        missing.
        """
        ip = self._resolve(who)
        if not ip:
            self.cmd_help("device")
            return
        ports = [(p, d) for p, d in self.DEVICE_PORTS]
        for tok in (extra or "").replace(",", " ").split():
            if tok.isdigit() and int(tok) not in [p for p, _ in ports]:
                ports.append((int(tok), "extra"))

        print()
        print("  checking which ports %s is listening on ..." % ip,
              flush=True)
        self.sec.send(pack_ctrl({"op": "scan", "cidr": "%s/32" % ip,
                                 "ports": [p for p, _ in ports][:6]}))
        r = self._await("scan", 90)
        open_now = set()
        if r and not r.get("error"):
            for d in (r.get("found") or []):
                if d.get("ip") == ip:
                    open_now = set(d.get("ports") or [])
        # The agent caps a scan at six ports, so ask for the rest too.
        rest = [p for p, _ in ports][6:]
        if rest:
            self.sec.send(pack_ctrl({"op": "scan", "cidr": "%s/32" % ip,
                                     "ports": rest[:6]}))
            r2 = self._await("scan", 90)
            if r2 and not r2.get("error"):
                for d in (r2.get("found") or []):
                    if d.get("ip") == ip:
                        open_now |= set(d.get("ports") or [])

        if not open_now:
            print("  nothing is listening on %s on the usual ports." % ip)
            print("  It may still answer on something else:")
            print("    map <localport> %s:<port>" % ip)
            return

        print()
        print("  %-7s %-24s %s" % ("LOCAL", "USE", "WHAT IT IS"))
        print("  " + "-" * 62)
        made = 0
        for port, what in ports:
            if port not in open_now:
                continue
            lport = self._free_port(port)
            if not lport:
                continue
            before = len(self.maps)
            self.cmd_map(str(lport), "%s:%d" % (ip, port), quiet=True)
            if len(self.maps) == before:
                continue
            made += 1
            print("  %-7d %-24s %s"
                  % (lport, "127.0.0.1:%d" % lport, what))
        print("  " + "-" * 62)
        print("  %d port(s) forwarded from %s." % (made, ip))
        print("  Point any program at 127.0.0.1 on the local port shown.")
        if 44818 in open_now:
            print()
            print("  For RSLinx: Ethernet devices driver, Station 0 =")
            print("    127.0.0.1    (NOT the EtherNet/IP driver - it")
            print("                  browses by broadcast)")

    def cmd_rdp(self, who: str = "") -> None:
        ip = self._resolve(who) if who else self._server_ip()
        if not ip:
            print("  could not work out which PC to connect to - try")
            print("    rdp <ip>")
            return
        lport = self._free_port(13389)
        if not lport:
            print("  no free local port for the forward")
            return
        self.cmd_map(str(lport), "%s:3389" % ip, quiet=True)
        print()
        print("  Remote Desktop is forwarded:")
        print("      127.0.0.1:%d  ->  %s:3389" % (lport, ip))
        print()
        print("  Connect with EITHER:")
        print("      mstsc /v:127.0.0.1:%d" % lport)
        print("  or open Remote Desktop Connection and type:")
        print("      127.0.0.1:%d" % lport)
        print()
        print("  Local port 3389 is avoided on purpose: your own PC almost")
        print("  certainly runs Remote Desktop on it, so binding it fails.")
        print("  That is why a plain `map 3389` did not work.")
        print("  The plant PC must also have Remote Desktop enabled.")

    # Ports a PLC engineer actually needs at the controller's own address.
    # 44818 is CIP explicit messaging - RSLinx, FactoryTalk Linx and going
    # online in Studio 5000 all run over it, including browsing the
    # backplane, which is CIP routing inside the same TCP connection.
    CLAIM_PORTS = (44818, 2222, 80, 443, 502, 102, 22, 3389)

    def cmd_claim(self, who: str, extra: str = "") -> None:
        """Make the device answer at its REAL address on this PC.

        This is what lets RSLinx keep the controller's own IP.
        """
        ip = self._resolve(who)
        if not ip:
            self.cmd_help("claim")
            return
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            print("  %r is not an IPv4 address" % ip)
            return
        if not _is_admin():
            print("  claim needs administrator rights - it adds an address")
            print("  to this PC. Close this and re-run tnlink.exe")
            print("  with: right-click > Run as administrator.")
            return
        if ip in self.claimed:
            print("  %s is already claimed" % ip)
            return

        # The only safe test: does anything HERE answer at that address?
        # Being on the same subnet is not a reason to refuse - a /32 is a
        # local address and is never ARPed for on the wire.
        here = icmp_once(ip, 900)
        if here.get("ok"):
            print("  REFUSING: something on THIS PC's network already")
            print("  answers at %s (%d ms). Claiming it would shadow a real"
                  % (ip, here.get("rtt_ms", 0)))
            print("  device here. Use  plc %s  instead, which forwards to"
                  % ip)
            print("  127.0.0.1 and leaves your own network alone.")
            return
        for a in local_subnets():
            if a.get("ip") == ip:
                print("  REFUSING: %s is this PC's own address." % ip)
                return

        idx, ifname, why_not = pick_claim_interface()
        if not idx:
            print("  REFUSING to claim an address on this PC.")
            print("  %s." % why_not)
            print()
            print("  Adding an address with netsh switches a DHCP adapter")
            print("  to STATIC, and that setting outlives the address. It")
            print("  has already taken one machine off the internet, so")
            print("  this will not touch an adapter that matters.")
            print()
            print("  Use  plc %s  instead - it forwards to 127.0.0.1 and" % ip)
            print("  changes no adapter at all.")
            return
        print("  borrowing the address on %s (no DHCP, no default route)"
              % ifname)
        ok, why = _add_local_ip(idx, ip)
        if not ok:
            print("  could not add %s to this PC - %s" % (ip, why))
            return
        self.claimed.append(ip)
        # A freshly added IPv4 address is Tentative until Duplicate Address
        # Detection finishes, and a Tentative address CANNOT be bound -
        # bind fails with "The requested address is not valid in its
        # context". Measured elevated on 2026-10-21: Tentative at t+0.8s,
        # Preferred at t+3.7s. An earlier 5s budget was far too close to
        # that, and every forward would have failed silently.
        print("  waiting for Windows to accept the address "
              "(duplicate address detection) ...", flush=True)
        ready = False
        t0 = time.time()
        while time.time() - t0 < 25:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind((ip, 0))
                ready = True
            except OSError:
                pass
            finally:
                probe.close()
            if ready:
                break
            time.sleep(0.25)
        if not ready:
            print("  Windows never made %s usable (%.0fs). Removing it."
                  % (ip, time.time() - t0))
            _remove_local_ip(ip)
            if ip in self.claimed:
                self.claimed.remove(ip)
            return
        print("  address ready after %.1fs" % (time.time() - t0))

        ports = list(self.CLAIM_PORTS)
        for tok in (extra or "").split(","):
            tok = tok.strip()
            if tok.isdigit() and int(tok) not in ports:
                ports.append(int(tok))
        done, failed = [], []
        for port in ports:
            before = len(self.maps)
            self.cmd_map("%s:%d" % (ip, port), "%s:%d" % (ip, port),
                         quiet=True)
            (done if len(self.maps) > before else failed).append(port)

        print()
        print("  %s now answers on this PC, at its OWN address." % ip)
        print("  forwarded: %s" % ", ".join(str(p) for p in done))
        if failed:
            print("  not forwarded (port busy here): %s"
                  % ", ".join(str(p) for p in failed))
        print()
        print("  RSLinx Classic:")
        print("    Communications > Configure Drivers")
        print("    Add New > Ethernet devices     (NOT the EtherNet/IP")
        print("                                    driver - it browses by")
        print("                                    broadcast, which cannot")
        print("                                    cross a tunnel)")
        print("    Station 0:  %s" % ip)
        print()
        print("  FactoryTalk Linx:")
        print("    add an Ethernet driver, then add device %s" % ip)
        print()
        print("  Then RSWho shows the controller, and Studio 5000 can go")
        print("  online through it - browsing the backplane rides the same")
        print("  CIP connection on 44818.")
        print()
        print("  Windows ping to %s now answers LOCALLY and proves" % ip)
        print("  nothing - use this tool's  ping %s  for the real" % ip)
        print("  device. The address disappears on unclaim, on quit, and")
        print("  at the next reboot even if this program is killed.")

    def cmd_unclaim(self) -> None:
        if not self.claimed:
            print("  nothing claimed")
            return
        for ip in list(self.claimed):
            ok, why = _remove_local_ip(ip)
            print("  %s %s"
                  % (ip, "removed" if ok else "NOT removed - " + why))
            if ok:
                self.claimed.remove(ip)

    def cmd_status(self) -> None:
        if self.tunnel is not None and getattr(self.tunnel, "stats", None):
            st = self.tunnel.stats
            print("  frames      : %d out, %d in" % (st.get("out", 0),
                                                     st.get("in", 0)))
        print("  relay       : %s" % ("configured" if getattr(self, "relay", "") else "direct"))
        print("  server      : %s" % (self.info.get("host") or "?"))
        print("  forwards    : %d" % len(self.maps))
        for m in self.maps:
            print("                %s -> %s" % (m["local"], m["remote"]))
        print("  claimed IPs : %s" % (", ".join(self.claimed) or "none"))
        print("  devices     : %d from the last scan" % len(self.devices))

    # -- console ----------------------------------------------------------
    HELP = {
        "ping": ["ping <ip|#> [count]",
                 "  ICMP echo sent from the SERVER's network, so it proves",
                 "  the device answers there - not from your PC.",
                 "    ping 192.168.10.240        ping 3"],
        "devices": ["devices [cidr] [ports]",
                    "  Scan the server's networks and number what answers,",
                    "  so you can then say 1 instead of an address.",
                    "    devices",
                    "    devices 192.168.10.0/24",
                    "    devices 192.168.10.0/24 44818,502"],
        "scan": ["scan <cidr> [port,port]",
                 "  Same as devices but for one range you name.",
                 "    scan 192.168.10.0/24 44818"],
        "map": ["map <localport> <ip|#>:<port>",
                "  Forward a local port to a device port. Then point any",
                "  Windows program at 127.0.0.1:<localport>.",
                "    map 44818 192.168.10.240:44818",
                "    map 44818 3:44818",
                "  Pick a local port nothing else uses - 3389 is normally",
                "  taken by your own Remote Desktop."],
        "rdp": ["rdp [ip|#]",
                "  Remote Desktop to the plant PC (or a device), on a free",
                "  local port, and prints the exact mstsc command.",
                "    rdp            rdp 2"],
        "claim": ["claim <ip|#> [extra,ports]",
                  "  THE ONE FOR RSLINX. Gives this PC the device's own",
                  "  address and forwards its ports, so RSLinx and Studio",
                  "  5000 keep the controller's real IP. Needs admin.",
                  "    claim 192.168.10.240       claim 1       claim 1 4840",
                  "  Forwards 44818, 2222, 80, 443, 502, 102, 22, 3389.",
                  "  Refused only if something HERE already answers at that",
                  "  address. TCP only - Windows ping to it answers locally",
                  "  and proves nothing; use this tool's ping instead.",
                  "  Removed by unclaim, by quit, and at the next reboot."],
        "bridge": ["bridge  [cidr]   |   bridge ip <a.b.c.d/n>   |",
                   "bridge dhcp      |   bridge down",
                   "  THE REAL ONE. Shares the plant PC's network with",
                   "  this PC by bridging it, so this PC joins that",
                   "  Ethernet segment. The PLC keeps its own address and",
                   "  nothing about the plant network changes.",
                   "  Then PLC software finds devices exactly as on site.",
                   "  Needs administrator rights at both ends.",
                   "    bridge                 share the plant network",
                   "    bridge ip 192.168.10.77/24",
                   "    bridge dhcp            or let their DHCP answer",
                   "    bridge down            remove it everywhere"],
        "tunnel": ["tunnel   |   tunnel down",
                   "  THE REAL ONE. Creates a TN-Link network adapter on",
                   "  this PC and on the plant PC, and routes the plant",
                   "  network over it. The PLC keeps its own address, so",
                   "  RSLinx and Studio 5000 need no special settings -",
                   "  just enter the controller's real IP.",
                   "  Needs administrator rights at both ends.",
                   "  Refused if this PC is already on the plant's subnet."],
        "device": ["device <ip|#> [extra ports]",
                   "  Forward EVERYTHING a device is listening on - web",
                   "  page, EtherNet/IP, Modbus, SSH, RDP, VNC and more -",
                   "  so any program can reach it, not just PLC software.",
                   "    device 192.168.10.240      device 3",
                   "    device 3 1883 4840         (add your own ports)"],
        "plc": ["plc <ip|#> [port]",
                "  Forward a PLC's EtherNet/IP port (44818) and print the",
                "  exact RSLinx / FactoryTalk Linx settings.",
                "    plc 192.168.10.240        plc 1",
                "  Use the Ethernet devices (static IP) driver and enter",
                "  127.0.0.1 - the EtherNet/IP driver browses by broadcast,",
                "  which cannot cross a tunnel."],
        "maps": ["maps", "  List the active forwards."],
        "status": ["status", "  Session, forwards, claimed addresses."],
        "info": ["info", "  The server's networks and what is reachable."],
    }

    def cmd_help(self, topic: str = "") -> None:
        if topic and topic in self.HELP:
            for ln in self.HELP[topic]:
                print("  " + ln)
            return
        print("  COMMAND                      WHAT IT DOES")
        print("  " + "-" * 62)
        print("  devices                      find the PLCs and show them")
        print("                               numbered - START HERE")
        print("  ping <ip|#> [count]          ICMP from the server's network")
        print("  map <lport> <ip|#>:<port>    forward a port to your PC")
        print("  bridge                       JOIN the plant network -")
        print("                               PLC keeps its own IP (admin)")
        print("  bridge ip <a.b.c.d/n>        address on that network")
        print("  bridge dhcp                  ...or ask their DHCP")
        print("  bridge down                  remove it again")
        print("  device <ip|#>                reach a device - all its")
        print("                               ports, any kind of device")
        print("  plc <ip|#>                   just the PLC's CIP port")
        print("  rdp [ip|#]                   Remote Desktop, port picked")
        print("                               for you")
        print("  claim <ip|#>                 device answers at its REAL")
        print("                               address here - USE THIS FOR")
        print("                               RSLINX / STUDIO 5000 (admin)")
        print("  unclaim                      undo claim")
        print("  scan <cidr> [ports]          scan one range")
        print("  maps | status | info         what is set up")
        print("  help <command>               detail and examples")
        print("  quit")
        print("  " + "-" * 62)
        print("  Typical session:")
        print("      devices")
        print("      bridge                join the plant network")
        print("      bridge dhcp           get an address on it")
        print("                            then point RSLinx straight at")
        print("                            the PLC's real address")
        print("      rdp                   Remote Desktop to the plant PC")

    MENU = [
        ("1", "Connect to a PLC  (recommended)",
         "RSLinx + Studio 5000"),
        ("2", "Find devices on the plant network", ""),
        ("3", "Remote Desktop to the plant PC", ""),
        ("4", "Reach a device  (all its ports)", "any device, not just PLCs"),
        ("5", "What is set up right now", ""),
        ("6", "Share the whole network", "reach ANY device"),
        ("8", "Share at Layer 2 (bridge)", "advanced - browsing only"),
        ("7", "Stop everything", ""),
        ("0", "Quit", ""),
    ]

    def show_menu(self) -> None:
        print()
        print("  WHAT DO YOU WANT TO DO?")
        print("  " + "-" * 60)
        for key, label, note in self.MENU:
            print("   %s) %-42s %s" % (key, label, note))
        print("  " + "-" * 60)
        print("  Type a number, or a command (type  help  for commands).")

    def run_menu(self, choice: str) -> bool:
        """True if the choice was handled as a menu item."""
        if choice == "1":
            self.menu_connect_plc()
            return True
        if choice == "2":
            self.cmd_devices()
            return True
        if choice == "3":
            self.cmd_rdp()
            return True
        if choice == "4":
            if not self.devices:
                self.cmd_devices()
            try:
                who = input("  device number or IP: ").strip()
            except (EOFError, KeyboardInterrupt):
                return True
            if who:
                self.cmd_device(who)
            return True
        if choice == "5":
            self.cmd_status()
            return True
        if choice == "6":
            self.menu_share_network()
            return True
        if choice == "8":
            self.menu_share_bridge()
            return True
        if choice == "7":
            if self.tunnel:
                self.cmd_bridge_down()
            for m in list(self.maps):
                pass
            self.maps = []
            print("  stopped")
            return True
        return False

    #: What a plant network actually contains. 2026-10-28, reported: "we
    #: might have different devices in the network including HMI, including
    #: IFM blocks, siemens PLC or other ethernet IP devices that need to be
    #: connected, we should not assume that is always an allenbradley one".
    #: Scanning only 44818 found Allen-Bradley and nothing else - a Siemens
    #: CPU, an ifm IO-Link master and a panel were all invisible.
    FIND_PORTS = (44818, 502, 102, 80, 443, 2222, 8080)

    #: How a device introduces itself, from the ports that answered.
    PORT_MEANING = (
        (44818, "EtherNet/IP"),
        (2222, "EtherNet/IP"),
        (102, "Siemens S7"),
        (502, "Modbus TCP"),
        (443, "web"),
        (80, "web"),
        (8080, "web"),
    )

    def describe_device(self, d: dict) -> str:
        ports = set(d.get("ports") or [])
        names = []
        for port, label in self.PORT_MEANING:
            if port in ports and label not in names:
                names.append(label)
        return ", ".join(names) or "reachable"

    def find_plcs(self) -> list:
        """Scan EVERY plant network, for every device - not just a PLC.

        2026-10-28. This scanned 44818 only and stopped at the first
        network that answered, then the caller took plcs[0]. So on a plant
        with an HMI, a drive and a Siemens CPU the operator was shown one
        Allen-Bradley address and never asked. Both of those shortcuts are
        gone: every LAN is scanned, every answering device is kept, and the
        caller lists them.
        """
        lans = [a for a in (self.info.get("subnets") or [])
                if a.get("kind", "lan") == "lan"]
        if not lans:
            return []
        found = []
        for a in lans:
            self.sec.send(pack_ctrl({"op": "scan", "cidr": a["cidr"],
                                     "ports": list(self.FIND_PORTS)}))
            print("  scanning %s for devices ..." % a["cidr"], flush=True)
            r = self._await("scan", 240)
            if not r or r.get("error"):
                continue
            for d in (r.get("found") or []):
                if d.get("ports") and d.get("ip") not in [f["ip"] for f in found]:
                    found.append(d)
        if found:
            self.devices = found
        return found

    def menu_connect_plc(self) -> None:
        """One keystroke from here to Studio 5000 going online.

        Finds controllers, forwards the CIP port, and prints the exact
        settings - rather than leaving the operator to work out which of
        RSLinx's drivers can cross a tunnel.
        """
        plcs = [d for d in self.devices if d.get("ports")]
        if not plcs:
            plcs = self.find_plcs()
        if not plcs:
            print()
            print("  nothing answered on the plant network.")
            print("  If you know the address:  plc <ip>")
            return
        # ALWAYS list. One device used to be chosen silently, which is how
        # an operator ends up connected to something they did not pick.
        print()
        print("  devices found:")
        for i, d in enumerate(plcs, 1):
            print("   %d) %-16s %-14s %s"
                  % (i, d["ip"], self.describe_device(d),
                     ("%d ms" % d["rtt_ms"]) if d.get("rtt_ms") is not None else ""))
        print()
        try:
            sel = input("  which one (1-%d, Enter to cancel)> " % len(plcs)).strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not sel:
            return
        if not (sel.isdigit() and 1 <= int(sel) <= len(plcs)):
            print("  not a listed number")
            return
        chosen = plcs[int(sel) - 1]
        port = "44818" if 44818 in (chosen.get("ports") or [])             else str(sorted(chosen.get("ports") or [44818])[0])
        self.cmd_plc(chosen["ip"], port)

    def menu_share_network(self) -> None:
        """Press 6 and it shares the network. No ceremony."""
        self.cmd_tunnel("")
        return

    def menu_share_bridge(self) -> None:
        """The Layer 2 bridge. Kept for broadcast browsing only."""
        lans = [a for a in (self.info.get("subnets") or [])
                if a.get("kind", "lan") == "lan"]
        if not lans:
            print("  the plant PC reported no network to share")
            return

        pick = lans[0]["cidr"]
        if len(lans) > 1:
            print()
            print("  Which network?")
            for i, a in enumerate(lans, 1):
                print("   %d) %-18s %s"
                      % (i, a["cidr"], str(a.get("desc") or "")[:34]))
            try:
                sel = (input("  number [1]: ").strip() or "1")
            except (EOFError, KeyboardInterrupt):
                return
            if not (sel.isdigit() and 1 <= int(sel) <= len(lans)):
                print("  not a listed number")
                return
            pick = lans[int(sel) - 1]["cidr"]

        self.cmd_bridge(pick)
        if not self.tunnel:
            return
        self.menu_address(pick)

    def menu_address(self, cidr: str = "") -> None:
        """Automatic or typed - and say plainly what happened either way."""
        print()
        print("  Address for this PC on that network:")
        print("   1) Automatic          (recommended)")
        print("   2) Type one myself")
        try:
            how = (input("  number [1]: ").strip() or "1")
        except (EOFError, KeyboardInterrupt):
            return

        if how == "2":
            example = "192.168.10.77/24"
            if cidr and "/" in cidr:
                base = cidr.split("/")[0].rsplit(".", 1)[0]
                example = "%s.77/%s" % (base, cidr.split("/")[1])
            try:
                spec = input("  address [%s]: " % example).strip() or example
            except (EOFError, KeyboardInterrupt):
                return
            self.cmd_bridge_ip(spec)
        else:
            print("  asking the network for an address ...", flush=True)
            self.cmd_bridge_ip("dhcp")

        got = self.tunnel.address() if self.tunnel else ""
        if not got and how != "2":
            # Do not make them walk the menu again just to type one.
            example = "192.168.10.77/24"
            if cidr and "/" in cidr:
                base = cidr.split("/")[0].rsplit(".", 1)[0]
                example = "%s.77/%s" % (base, cidr.split("/")[1])
            print()
            print("  No DHCP on that network. Type an address instead:")
            try:
                spec = input("  address [%s]: " % example).strip() or example
            except (EOFError, KeyboardInterrupt):
                return
            self.cmd_bridge_ip(spec)
            got = self.tunnel.address() if self.tunnel else ""
        print()
        if got:
            print("  This PC is now %s on that network." % got)
            print("  Everything on it is reachable at its own address -")
            print("  try:   ping <a device>   from a command prompt.")
        else:
            print("  Still no address - try option 6 again.")

    def console(self) -> int:
        print()
        print("  TN-Link - client  (%s)" % VERSION_FULL)
        print("  " + "-" * 62)
        self.cmd_info()
        print("  " + "-" * 62)
        self.show_menu()
        print()
        while self.alive:
            try:
                line = input("tnlink> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            parts = line.split()
            cmd, args = parts[0].lower(), parts[1:]
            try:
                if cmd == "0":
                    break
                if len(parts) == 1 and cmd.isdigit():
                    if self.run_menu(cmd):
                        # One short line, not the whole menu again -
                        # twenty lines after every action buries the
                        # answer the operator just asked for.
                        print()
                        print("  [ m = menu   help = commands   0 = quit ]")
                        continue
                    print("  %r is not on the menu" % cmd)
                    continue
                if cmd in ("quit", "exit", "q"):
                    break
                if cmd in ("menu", "m"):
                    self.show_menu()
                    continue
                if cmd in ("help", "?", "h"):
                    self.cmd_help(args[0].lower() if args else "")
                elif cmd == "info":
                    self.cmd_info()
                elif cmd == "status":
                    self.cmd_status()
                elif cmd in ("devices", "dev", "d"):
                    self.cmd_devices(args[0] if args else "",
                                     args[1] if len(args) > 1 else "")
                elif cmd == "ping":
                    if not args:
                        self.cmd_help("ping")
                    else:
                        ip = self._resolve(args[0])
                        if ip:
                            self.cmd_ping(ip, int(args[1])
                                          if len(args) > 1 else 4)
                elif cmd == "scan":
                    if not args:
                        self.cmd_help("scan")
                    else:
                        ports = ([int(p) for p in args[1].split(",")
                                  if p.strip()] if len(args) > 1 else [])
                        self.cmd_scan(args[0], ports)
                elif cmd == "map":
                    if len(args) < 2:
                        self.cmd_help("map")
                    elif ":" not in args[1]:
                        print("  the target needs a port, like  map 44818 1:44818")
                        self.cmd_help("map")
                    else:
                        who, rport = args[1].rsplit(":", 1)
                        ip = self._resolve(who)
                        if ip:
                            self.cmd_map(args[0], "%s:%s" % (ip, rport))
                elif cmd == "bridge":
                    if args and args[0].lower() == "ip":
                        self.cmd_bridge_ip(args[1] if len(args) > 1 else "")
                    elif args and args[0].lower() == "dhcp":
                        self.cmd_bridge_ip("dhcp")
                    else:
                        self.cmd_bridge(args[0] if args else "")
                elif cmd == "tunnel":
                    self.cmd_tunnel(args[0] if args else "")
                elif cmd in ("device", "dev2"):
                    self.cmd_device(args[0] if args else "",
                                    " ".join(args[1:]))
                elif cmd == "plc":
                    self.cmd_plc(args[0] if args else "",
                                 args[1] if len(args) > 1 else "44818")
                elif cmd == "rdp":
                    self.cmd_rdp(args[0] if args else "")
                elif cmd == "claim":
                    if not args:
                        self.cmd_help("claim")
                    else:
                        self.cmd_claim(args[0],
                                       args[1] if len(args) > 1 else "")
                elif cmd == "unclaim":
                    self.cmd_unclaim()
                elif cmd == "maps":
                    if not self.maps:
                        print("  no forwards yet - try: devices")
                    for m in self.maps:
                        print("  %s -> %s" % (m["local"], m["remote"]))
                else:
                    print("  unknown command %r - type help" % cmd)
            except Exception as exc:
                print("  %s: %s" % (type(exc).__name__, exc))
        if self.tunnel:
            print("  removing the TN-Link adapter ...")
            try:
                if hasattr(self.tunnel, "bridge_with"):
                    self.cmd_bridge_down()
                else:
                    self.cmd_tunnel_down()
            except Exception:                               # noqa: BLE001
                pass
        if self.claimed:
            print("  removing claimed addresses ...")
            self.cmd_unclaim()
        self.alive = False
        if self.sec:
            self.sec.close()
        return 0


# --------------------------------------------------------------------------
#: Where the remembered role lives. One file, beside the other per-user
#: state, so a tray autostart does not ask again every boot.
def _settings_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "TrustNodeLink")
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        pass
    return os.path.join(folder, "settings.json")


def _machine_anchor() -> str:
    """Something that survives a rename but not a sysprepped clone.

    Windows keeps MachineGuid across renames, in-place upgrades and hardware
    changes; sysprep regenerates it, which is exactly the clone case. Empty
    when it cannot be read, and the caller falls back to the hostname.
    """
    try:
        import winreg                                    # noqa: PLC0415
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            return str(winreg.QueryValueEx(k, "MachineGuid")[0] or "").strip().lower()
    except Exception:
        return ""


def _load_settings() -> dict:
    try:
        import json as _json
        with open(_settings_path(), "r", encoding="utf-8") as fh:
            data = _json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_settings(data: dict) -> bool:
    try:
        import json as _json
        with open(_settings_path(), "w", encoding="utf-8") as fh:
            _json.dump(data, fh, indent=2)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# The address book.
#
# 2026-10-31, requested: "the connection id also the same so we can always
# reconnect ... shows the recent connections and also save them a favorite
# and rename the connection if needed".
#
# A pairing CODE is deliberately one-shot and rotates after every use - that
# is what stops a leaked code being replayed, and it must stay that way. So a
# saved connection is keyed by the other end's permanent UNIT ID instead, and
# the code is only ever the handshake. That gives a connection an identity
# that outlives the code, which is what "always be able to reconnect" needs.
#
# Entries are keyed by unit id, so re-pairing with a machine updates the one
# the operator already named instead of growing a second row for it.
def connections() -> list:
    """Known connections, favourites first, then most recently used."""
    rows = _load_settings().get("connections")
    rows = [r for r in (rows or []) if isinstance(r, dict) and r.get("unit")]
    rows.sort(key=lambda r: (0 if r.get("favorite") else 1,
                             -float(r.get("last_utc") or 0)))
    return rows


def remember_connection(unit: str, *, host: str = "", address: str = "",
                        role: str = "") -> dict:
    """Record a successful connection. Never overwrites the operator's name."""
    unit = str(unit or "").strip().upper()
    if not unit:
        return {}
    import time as _t
    data = _load_settings()
    rows = [r for r in (data.get("connections") or []) if isinstance(r, dict)]
    row = None
    for r in rows:
        if str(r.get("unit") or "").strip().upper() == unit:
            row = r
            break
    if row is None:
        row = {"unit": unit, "name": "", "favorite": False, "uses": 0}
        rows.append(row)
    if host:
        row["host"] = str(host)
    if address:
        row["address"] = str(address)
    if role:
        row["role"] = str(role)
    row["uses"] = int(row.get("uses") or 0) + 1
    row["last_utc"] = _t.time()
    # Keep the list useful rather than complete: favourites always stay, and
    # the 50 most recent of the rest. An address book nobody can read is the
    # same as not having one.
    keep = [r for r in rows if r.get("favorite")]
    rest = sorted([r for r in rows if not r.get("favorite")],
                  key=lambda r: -float(r.get("last_utc") or 0))[:50]
    data["connections"] = keep + rest
    _save_settings(data)
    return dict(row)


def rename_connection(unit: str, name: str) -> bool:
    """The operator's own label for a machine. Blank restores the host name."""
    unit = str(unit or "").strip().upper()
    data = _load_settings()
    for r in (data.get("connections") or []):
        if isinstance(r, dict) and str(r.get("unit") or "").upper() == unit:
            r["name"] = str(name or "").strip()[:60]
            return _save_settings(data)
    return False


def favorite_connection(unit: str, on: bool = True) -> bool:
    unit = str(unit or "").strip().upper()
    data = _load_settings()
    for r in (data.get("connections") or []):
        if isinstance(r, dict) and str(r.get("unit") or "").upper() == unit:
            r["favorite"] = bool(on)
            return _save_settings(data)
    return False


def forget_connection(unit: str) -> bool:
    unit = str(unit or "").strip().upper()
    data = _load_settings()
    rows = [r for r in (data.get("connections") or [])
            if isinstance(r, dict) and str(r.get("unit") or "").upper() != unit]
    data["connections"] = rows
    return _save_settings(data)


# --------------------------------------------------------------------------
# Trusted pairs.
#
# 2026-10-31, requested: "everytime if I want to connect I can connect to the
# same computer, without need to get new code all the time ... the connection
# to the server id should be possible only entering once the code".
#
# The code STAYS one-shot and still rotates - that is what stops a code read
# out over the phone being replayed by whoever else heard it. What changes is
# that a successful code pairing now also mints a long random per-pair key.
# The server keeps only its HASH, the client keeps the key, and both file it
# under the other end's permanent unit id. Later connections authenticate
# with that key instead of a code.
#
# This is the ssh known_hosts/authorized_keys shape, and it is strictly
# better than remembering the last code, because:
#   * the key is per-pair - it grants one engineer access, not everyone who
#     was told the code;
#   * it is never spoken aloud or pasted into a chat;
#   * the operator can revoke one engineer (forget_trust) without changing
#     anything for the others.
def _trust_hash(token: str) -> str:
    import hashlib as _h
    return _h.sha256(("tnlink-trust-v1:" + str(token or "")).encode()).hexdigest()


def trusted_units() -> list:
    """Who may reconnect to THIS machine without a code."""
    rows = _load_settings().get("trusted")
    return [r for r in (rows or []) if isinstance(r, dict) and r.get("unit")]


def grant_trust(unit: str, host: str = "") -> str:
    """Mint a per-pair key for `unit`. Returns the key - stored hashed here."""
    import secrets as _s
    import time as _t
    unit = str(unit or "").strip().upper()
    if not unit:
        return ""
    token = _s.token_urlsafe(32)
    data = _load_settings()
    rows = [r for r in (data.get("trusted") or [])
            if isinstance(r, dict) and str(r.get("unit") or "").upper() != unit]
    rows.append({"unit": unit, "host": str(host or ""),
                 "hash": _trust_hash(token), "added_utc": _t.time()})
    data["trusted"] = rows
    _save_settings(data)
    return token


def trusted_secret(unit: str, token: str) -> bool:
    """Does this unit's key match what we stored? Constant-time."""
    import hmac as _hm
    unit = str(unit or "").strip().upper()
    want = _trust_hash(token)
    for r in trusted_units():
        if str(r.get("unit") or "").upper() == unit:
            return _hm.compare_digest(str(r.get("hash") or ""), want)
    return False


def is_trusted(unit: str) -> bool:
    unit = str(unit or "").strip().upper()
    return any(str(r.get("unit") or "").upper() == unit for r in trusted_units())


def forget_trust(unit: str) -> bool:
    """Revoke one engineer. They need a fresh code to come back."""
    unit = str(unit or "").strip().upper()
    data = _load_settings()
    before = len(data.get("trusted") or [])
    data["trusted"] = [r for r in (data.get("trusted") or [])
                       if isinstance(r, dict)
                       and str(r.get("unit") or "").upper() != unit]
    _save_settings(data)
    return len(data["trusted"]) != before


def remember_trust(unit: str, token: str) -> bool:
    """Client side: keep the key the server gave us, against its unit id."""
    unit = str(unit or "").strip().upper()
    data = _load_settings()
    for r in (data.get("connections") or []):
        if isinstance(r, dict) and str(r.get("unit") or "").upper() == unit:
            r["token"] = str(token or "")
            return _save_settings(data)
    return False


def trust_token_for(unit: str) -> str:
    """Client side: the key for a saved server, or "" to fall back to a code."""
    unit = str(unit or "").strip().upper()
    for r in (_load_settings().get("connections") or []):
        if isinstance(r, dict) and str(r.get("unit") or "").upper() == unit:
            return str(r.get("token") or "")
    return ""


#: Sent before the handshake so each end can choose the right secret. The
#: unit id is not a secret - it is an opaque label - but the KEY never
#: crosses the wire, only the id it is filed under.
PREAMBLE = 48
PREAMBLE_MAGIC = b"TNU1"


def _preamble_send(sock, unit: str) -> None:
    body = str(unit or "").encode("ascii", "replace")[:PREAMBLE - len(PREAMBLE_MAGIC)]
    sock.sendall(PREAMBLE_MAGIC + body.ljust(PREAMBLE - len(PREAMBLE_MAGIC), b"\x00"))


def _preamble_recv(sock) -> str:
    blob = _recv_exact(sock, PREAMBLE)
    if not blob.startswith(PREAMBLE_MAGIC):
        raise AuthError("this is not a TN-Link endpoint (bad preamble)")
    return blob[len(PREAMBLE_MAGIC):].rstrip(b"\x00").decode("ascii", "replace")


def remember_next_code(unit: str, code: str) -> bool:
    """Keep the code this server will expect from us next time.

    Stored against the server's permanent unit id, so it survives that
    machine being renamed and does not depend on its address. The row is
    created if the address book has not seen it yet - the first pairing is
    the one that hands a next code out, so requiring the row to exist first
    would drop exactly the one that matters.
    """
    unit = str(unit or "").strip().upper()
    code = str(code or "").strip()
    if not unit or not code:
        return False
    data = _load_settings()
    rows = [r for r in (data.get("connections") or []) if isinstance(r, dict)]
    for r in rows:
        if str(r.get("unit") or "").upper() == unit:
            r["next_code"] = code
            data["connections"] = rows
            return _save_settings(data)
    rows.append({"unit": unit, "name": "", "favorite": False,
                 "uses": 0, "next_code": code})
    data["connections"] = rows
    return _save_settings(data)


def next_code_for(unit: str) -> str:
    """The stored code for a saved server, or "" to ask the operator."""
    unit = str(unit or "").strip().upper()
    for r in (_load_settings().get("connections") or []):
        if isinstance(r, dict) and str(r.get("unit") or "").upper() == unit:
            return str(r.get("next_code") or "")
    return ""


def connection_label(row: dict) -> str:
    """What to show: the operator's name, else the host, else the id."""
    if not isinstance(row, dict):
        return ""
    return (str(row.get("name") or "").strip()
            or str(row.get("host") or "").strip()
            or str(row.get("unit") or "").strip())


def unit_id() -> str:
    """This installation's own identifier, stable across restarts.

    2026-10-28, requested: "the app should have a unit communication ID on
    the server". The connection CODE is deliberately short-lived - it is
    regenerated, it expires, and it is how access is revoked. An engineer
    still needs to know WHICH plant PC they are looking at across many
    sessions, and a code that changes cannot answer that.

    So the unit id is written once, beside the remembered role, and never
    changes. It identifies the machine; it does not authorise anything, and
    knowing it grants nothing - pairing still needs the current code.
    """
    try:
        import json as _json
        path = _settings_path()
        data = {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = _json.load(fh) or {}
        except Exception:
            data = {}
        uid = str(data.get("unit_id") or "").strip()
        # 2026-10-28, clarified: "every server should have their own unit
        # ID". A random id alone is not enough - settings.json travels when
        # someone copies a folder or clones a machine, and two plants would
        # then answer to the same id. It is therefore bound to THIS
        # installation: the stored id is kept only while the host it was
        # minted on still matches, and re-minted otherwise.
        host = ""
        try:
            import socket as _sk
            host = (_sk.gethostname() or "").strip().lower()
        except Exception:
            host = ""
        # 2026-10-31, requested: "every computer where it is installed keeps
        # their own id forever". It did not. The id was tied to the HOSTNAME,
        # so renaming the PC - which customers do, and IT departments do in
        # bulk - threw the identity away and minted a new one. Every saved
        # connection pointing at that machine went stale at the same moment.
        #
        # The machine GUID is the right anchor: Windows keeps it across
        # renames, upgrades and new network cards, and sysprep REPLACES it
        # when an image is cloned properly - which is the case the hostname
        # check was really aiming at. Hostname stays as the fallback for
        # non-Windows and for a registry we cannot read.
        anchor = _machine_anchor() or host
        stored_anchor = str(data.get("unit_anchor") or "").strip().lower()
        if uid and not stored_anchor:
            # Minted before this change. Adopt it rather than re-mint: the id
            # an operator has already written down must not change under them.
            data["unit_anchor"] = anchor
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    _json.dump(data, fh, indent=2)
            except Exception:
                pass
            return uid
        if uid and anchor and stored_anchor and stored_anchor != anchor:
            uid = ""                       # a genuine clone - mint a new one
        if not uid:
            import uuid as _uuid
            raw = _uuid.uuid4().hex[:12].upper()
            uid = "TNU-%s-%s-%s" % (raw[0:4], raw[4:8], raw[8:12])
            data["unit_id"] = uid
            data["unit_host"] = host
            data["unit_anchor"] = anchor
            with open(path, "w", encoding="utf-8") as fh:
                _json.dump(data, fh, indent=2)
        return uid
    except Exception:
        return ""


def remembered_role() -> str:
    try:
        import json as _json
        with open(_settings_path(), "r", encoding="utf-8") as fh:
            return str((_json.load(fh) or {}).get("role") or "")
    except Exception:
        return ""


def remember_role(role: str) -> None:
    try:
        import json as _json
        data = {}
        try:
            with open(_settings_path(), "r", encoding="utf-8") as fh:
                data = _json.load(fh) or {}
        except Exception:
            data = {}
        data["role"] = str(role or "")
        with open(_settings_path(), "w", encoding="utf-8") as fh:
            _json.dump(data, fh, indent=2)
    except Exception:
        pass


def ask_role() -> str:
    """One exe, two jobs - ask once, then remember.

    2026-10-28, requested: "we need to have one exe app that can be used for
    client and server". The build shipped tnlink-server.exe AND
    tnlink-client.exe and read the role off the file name, so a customer had
    to be told WHICH to download, and renaming it silently changed what it
    did. One binary cannot guess: the same laptop is the engineer today and
    could sit beside a PLC tomorrow. So it asks, once, in the operator's own
    words, and remembers the answer.
    """
    print()
    print("  Which is this computer?")
    print()
    print("    1  At the plant - it stays here, beside the PLC, and shares")
    print("       its network or a device with an engineer.")
    print("    2  The engineer - it connects out to a plant to work on it.")
    print()
    while True:
        pick = input("  1 or 2> ").strip()
        if pick in ("1", "server", "plant"):
            remember_role("server")
            return "server"
        if pick in ("2", "client", "engineer"):
            remember_role("client")
            return "client"
        print("  Type 1 or 2.")


def main(argv=None) -> int:
    role_default = None
    exe = os.path.basename(sys.argv[0]).lower()
    if "server" in exe:
        role_default = "server"
    elif "client" in exe:
        role_default = "client"
    else:
        # ONE exe: the name says nothing, so use what this machine chose
        # last time. Still empty means a first run, and main() asks below -
        # after argparse, so --role on the command line always wins.
        role_default = remembered_role() or None

    ap = argparse.ArgumentParser(
        prog="tnlink", description="TrustNode Link prototype")
    ap.add_argument("--role", choices=("server", "client"),
                    default=role_default,
                    help="defaults to whichever the exe name says")
    ap.add_argument("--code", default="",
                    help="Connection Code (server generates one if omitted)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--bind", default="0.0.0.0", help="server: listen address")
    ap.add_argument("--allow", action="append", default=[],
                    help="server: extra CIDR the client may reach "
                         "(default: this PC's own private subnets)")
    # 2026-10-28, requested: "the connection should have a limit of 6
    # hours". An hour is shorter than a commissioning visit, so the session
    # died in the middle of the work it was opened for.
    ap.add_argument("--minutes", type=int, default=360,
                    help="server: session lifetime, 0 = no limit")
    ap.add_argument("--no-firewall", action="store_true",
                    help="server: do not touch the firewall at all")
    ap.add_argument("--remove-firewall", action="store_true",
                    help="server: delete the rule and exit (needs an "
                         "elevated prompt)")
    ap.add_argument("--server", default="",
                    help="client: host[:port] of the server, or a pasted "
                         "pairing line")
    ap.add_argument("--relay", default=DEFAULT_RELAY,
                    help="rendezvous host[:port] both sides dial out to "
                         "(default: %s)" % (DEFAULT_RELAY or "none"))
    ap.add_argument("--console", action="store_true",
                    help="force the console interface even when "
                         "double-clicked")
    ap.add_argument("--tray", action="store_true",
                    help="run in the notification area instead of a console "
                         "(the machine beside a PLC is left running, and a "
                         "console window that must stay open is how it gets "
                         "closed by accident)")
    ap.add_argument("--direct", action="store_true",
                    help="do not use a relay; listen/connect on the LAN")
    a = ap.parse_args(argv)

    # 2026-10-28, reported with a screenshot: the single exe refused to
    # start at all - "--role is required (this exe name does not say
    # which)". Demanding the role up front is the wrong shape anyway: the
    # same machine is the engineer today and may sit beside a PLC
    # tomorrow, and nobody should have to decide at a command line before
    # the program will open.
    #
    # With no --role the app simply STARTS, in the notification area, and
    # the operator picks from the menu: share this computer, or connect to
    # a plant. The role is remembered afterwards so a restart comes back
    # the way they left it.
    # 2026-10-28, second correction. Making the tray the DEFAULT broke the
    # working console app: `tnlink.exe` hid its own window and started a
    # service, so the interface and every command vanished. The console is
    # what works and what people use; the tray is opt-in with --tray.
    if not a.role:
        a.role = remembered_role() or ""

    # 2026-10-28, third correction, and the reasoning matters because I got
    # this wrong in both directions.
    #
    # Tray-by-default broke the console (it hid the window and started a
    # service whose constructors were wrong), so I made the console the
    # default again - and then double-clicking the exe gave a console
    # instead of the tray app that was asked for.
    #
    # The real rule is about HOW it was started. Double-clicked, with no
    # arguments at all, it is the tray app. Started from a prompt with
    # arguments, it is the console tool it has always been. --console
    # forces the old behaviour for anyone who wants it.
    _double_clicked = (len(sys.argv) == 1)
    if _double_clicked and not getattr(a, "console", False):
        a.tray = True
    if getattr(a, "tray", False):
        # The tray drives the SAME Server/Client this function builds, so
        # there is one implementation of connecting and sharing.
        try:
            import tnlink_service
        except Exception as exc:                        # noqa: BLE001
            print("  the tray needs tnlink_service.py: %s" % exc)
            return 2
        global _TRAY_MODE
        _TRAY_MODE = True
        # One app per machine. A second would fight the first for the relay
        # room, the adapter and the firewall rule, and the interface would
        # be told two different stories about the same session - so the
        # second instance shows the first one's window and leaves.
        # An instance told to keep its endpoint somewhere else is a
        # deliberate second one - that is how the end-to-end test runs a
        # plant and an engineer on a single machine.
        # One at a time, and the new start does NOT take over: the running
        # instance may be holding a live session, an adapter and a firewall
        # rule, and stopping it to start an identical copy would drop an
        # engineer mid-download.
        #
        # The MUTEX decides. It is held by exactly one process - the app
        # itself - and the kernel releases it however that process dies.
        # The process list is only used to name what is in the way, never
        # to make the decision: it cannot tell a real second instance
        # from this build's own unpacker, and letting it decide stopped
        # the exe from starting at all.
        if os.environ.get("TNLINK_API_FILE"):
            print("  second instance allowed (TNLINK_API_FILE is set)")
        elif not _claim_single_instance():
            others = _other_instances()
            print("  TN-Link is already running%s - opening its window."
                  % (" (pid %s)" % ", ".join(str(p) for p in others)
                     if others else ""))
            try:
                import tnlink_tray
                tnlink_tray.open_console_window()
            except Exception:
                pass
            return 0
        return tnlink_service.run_tray(a.role, a)
    # Console mode still needs to know which job to do. Ask only here -
    # never when --tray was given, and never when --role was.
    if not a.role:
        a.role = ask_role()
    if a.role == "server":
        if a.remove_firewall:
            ok, why = _fw_remove()
            print("  firewall: %s - %s" % ("removed" if ok else "NOT removed",
                                           why))
            return 0 if ok else 2
        code = a.code.strip() or new_code()
        relay = "" if a.direct else (a.relay or "").strip()
        return Server(code, a.allow, a.port, a.minutes, a.bind,
                      firewall=not a.no_firewall, relay=relay).serve()

    code = a.code.strip()
    relay = "" if a.direct else (a.relay or "").strip()

    if relay:
        # Nothing to know but the code: the rendezvous is baked in, and
        # both sides dial OUT to it. No address, no port, no firewall.
        if not code and _interactive():
            print()
            print("  TN-Link - client  (%s)" % VERSION_FULL)
            print("  " + "=" * 64)
            print()
            print("    Enter the Connection Code from tnlink.exe:")
            print()
            print("        TN-ABCDE-FGHIJ-KLMNO")
            print()
            print("    Connecting through the relay")
            print()
            try:
                code = input("  Connection Code : ").strip()
            except (EOFError, KeyboardInterrupt):
                return 2
        if not code:
            print("  No Connection Code given. Ask the person at the")
            print("  machine to start tnlink.exe - it prints one.")
            return 2
        host, port = split_hostport(relay, 7443)
    else:
        if not a.server and _interactive():
            print()
            print("  TN-Link - client  (%s)  DIRECT mode" % VERSION_FULL)
            print("  " + "=" * 64)
            print()
            print("    Paste the line from tnlink.exe, for example:")
            print()
            print("        192.168.0.161:51820#TN-ABCDE-FGHIJ-KLMNO")
            print()
            try:
                a.server = input("  Paste here : ").strip()
            except (EOFError, KeyboardInterrupt):
                return 2
        if not a.server:
            ap.error("--server host[:port] is required with --direct")

        # One pasted line carries address, port and code.
        target = a.server.strip()
        for sep in ("#", "|", "/"):
            if sep in target:
                target, pasted = target.split(sep, 1)
                code = code or pasted.strip()
                break
        host, port = split_hostport(target, a.port)
        if not host:
            print("  No server address given.")
            return 2
        if not code and _interactive():
            try:
                code = input("  Connection Code : ").strip()
            except (EOFError, KeyboardInterrupt):
                return 2
        if not code:
            print("  No Connection Code entered. Ask the person at the")
            print("  machine to start tnlink.exe - it prints one.")
            return 2

    c = Client(host, port, code)
    c.relay = relay
    print()
    if relay:
        print("  connecting ...", flush=True)
    else:
        print("  connecting to %s:%d ..." % (host, port), flush=True)
    try:
        c.connect()
    except AuthError as exc:
        print("  cannot pair: %s" % exc)
        print("  Check the Connection Code against the server window - it")
        print("  changes every time the server is restarted.")
        return 2
    except OSError as exc:
        print("  cannot reach %s:%d - %s"
              % (host, port, exc.strerror or exc))
        print()
        if relay:
            print("  That is the RELAY, not the machine. Check:")
            print("   1. this PC has internet access,")
            print("   2. outbound TCP %d is not blocked here," % port)
            print("   3. the relay is running on %s." % host)
        else:
            print("  Things that cause this, in the order worth checking:")
            print("   1. tnlink.exe is not running on that PC.")
            print("   2. The address is one the server listed as a VIRTUAL")
            print("      adapter - those exist only inside that PC.")
            print("   3. A firewall is blocking TCP %d, or the two PCs are"
                  % port)
            print("      not on the same network.")
        return 2
    return c.console()


def _selftest_vendor() -> int:
    """Prove the adapter driver travelled inside the exe.

    The build asserts this. Shipping a binary whose driver is missing
    fails only on the customer's machine, which is the worst place.
    """
    import os as _os
    base = getattr(sys, "_MEIPASS", None) or _os.path.dirname(
        _os.path.abspath(__file__))
    need = ["vendor/tap/OemVista.inf", "vendor/tap/tap0901.sys",
            "vendor/tap/tap0901.cat", "vendor/tap/tapinstall.exe"]
    missing = [n for n in need
               if not _os.path.isfile(_os.path.join(base, *n.split("/")))]
    if missing:
        print("VENDOR-MISSING: %s" % ", ".join(missing))
        return 2
    print("VENDOR-OK: %d driver file(s) bundled" % len(need))
    return 0


def cli_entry() -> int:
    if "--selftest-vendor" in sys.argv:
        return _selftest_vendor()
    """Every exit path leaves the message on screen.

    argparse raises SystemExit, a bad port raises OSError, and either one
    used to take the window with it. Nothing here changes behaviour when
    run from a terminal.
    """
    rc = 2
    try:
        rc = main()
    except KeyboardInterrupt:
        print("\n  stopped")
        rc = 0
    except SystemExit as exc:          # --help, or an argparse error
        rc = exc.code if isinstance(exc.code, int) else 0
    except OSError as exc:
        print("\n  Could not start: %s" % (exc.strerror or exc))
        if getattr(exc, "errno", None) in (48, 98) or "address" in str(exc).lower():
            print("  That port is already in use. Try --port 51821.")
    except Exception:
        import traceback
        traceback.print_exc()
        print("\n  Unexpected error above - please send this text.")
    _pause()
    return rc


if __name__ == "__main__":
    raise SystemExit(cli_entry())
