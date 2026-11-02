# -*- coding: utf-8 -*-
"""A LS-Link virtual network adapter, driven from Python with ctypes.

This is the piece that makes the remote PLC network behave like a local
one: a real Windows network adapter appears at each end, Windows does all
the TCP/IP work on both sides, and this program only shuttles raw IP
packets between the adapter and the encrypted link.

That is the whole reason no userspace TCP stack is needed, and why no
compiler, no Go toolchain and no VPN software are needed either.

The adapter's NAME and TUNNEL TYPE are supplied by us, so it shows in
Windows as "LS-Link" - the driver vendor's name appears nowhere in the
product, which its licence also requires (it forbids using their names to
promote anything built on it).

The driver binary is the prebuilt signed wintun.dll in vendor/, shipped
unmodified and used only through the documented API, which is exactly what
its Prebuilt Binaries Licence permits. See vendor/WINTUN-LICENSE.txt.

Everything here needs administrator rights, because creating a network
adapter does.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

# Ring buffer between us and the driver. Must be a power of two within the
# documented bounds; 4 MiB is generous for PLC traffic and costs nothing
# but address space.
MIN_RING = 0x20000
MAX_RING = 0x4000000
DEFAULT_RING = 0x400000
MAX_IP_PACKET = 0xFFFF

WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
ERROR_NO_MORE_ITEMS = 259


class WintunError(Exception):
    pass


def _dll_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    # Frozen into an exe, the DLL sits beside the extracted bundle.
    for base in (getattr(sys, "_MEIPASS", None), here):
        if not base:
            continue
        for rel in ("wintun.dll", os.path.join("vendor", "wintun.dll")):
            p = os.path.join(base, rel)
            if os.path.isfile(p):
                return p
    raise WintunError("wintun.dll not found - it must ship beside the exe")


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


_dll = None


def _load():
    global _dll
    if _dll is not None:
        return _dll
    d = ctypes.WinDLL(_dll_path())

    d.WintunCreateAdapter.restype = wintypes.HANDLE
    d.WintunCreateAdapter.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                      ctypes.POINTER(_GUID)]
    d.WintunOpenAdapter.restype = wintypes.HANDLE
    d.WintunOpenAdapter.argtypes = [wintypes.LPCWSTR]
    d.WintunCloseAdapter.restype = None
    d.WintunCloseAdapter.argtypes = [wintypes.HANDLE]
    d.WintunDeleteDriver.restype = wintypes.BOOL
    d.WintunDeleteDriver.argtypes = []
    d.WintunGetAdapterLUID.restype = None
    d.WintunGetAdapterLUID.argtypes = [wintypes.HANDLE,
                                       ctypes.POINTER(ctypes.c_ulonglong)]
    d.WintunGetRunningDriverVersion.restype = wintypes.DWORD
    d.WintunGetRunningDriverVersion.argtypes = []

    d.WintunStartSession.restype = wintypes.HANDLE
    d.WintunStartSession.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    d.WintunEndSession.restype = None
    d.WintunEndSession.argtypes = [wintypes.HANDLE]
    d.WintunGetReadWaitEvent.restype = wintypes.HANDLE
    d.WintunGetReadWaitEvent.argtypes = [wintypes.HANDLE]

    d.WintunReceivePacket.restype = ctypes.POINTER(ctypes.c_ubyte)
    d.WintunReceivePacket.argtypes = [wintypes.HANDLE,
                                      ctypes.POINTER(wintypes.DWORD)]
    d.WintunReleaseReceivePacket.restype = None
    d.WintunReleaseReceivePacket.argtypes = [wintypes.HANDLE,
                                             ctypes.POINTER(ctypes.c_ubyte)]
    d.WintunAllocateSendPacket.restype = ctypes.POINTER(ctypes.c_ubyte)
    d.WintunAllocateSendPacket.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    d.WintunSendPacket.restype = None
    d.WintunSendPacket.argtypes = [wintypes.HANDLE,
                                   ctypes.POINTER(ctypes.c_ubyte)]
    _dll = d
    return d


def driver_version() -> str:
    v = _load().WintunGetRunningDriverVersion()
    if not v:
        return "(not running)"
    return "%d.%d" % ((v >> 16) & 0xFFFF, v & 0xFFFF)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run(args, timeout=40) -> tuple:
    try:
        r = subprocess.run(args, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True,
                           errors="replace", timeout=timeout)
        return r.returncode == 0, (r.stdout or "").strip()
    except Exception as exc:                                # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)


class Adapter:
    """A LS-Link adapter. Created on enter, destroyed on exit."""

    def __init__(self, name: str = "LS-Link", tunnel_type: str = "LS-Link"):
        self.name = name
        self.tunnel_type = tunnel_type
        self.handle = None
        self.luid = 0
        self.index = 0

    def create(self) -> "Adapter":
        if not is_admin():
            raise WintunError("creating a network adapter needs "
                              "administrator rights")
        d = _load()
        h = d.WintunCreateAdapter(self.name, self.tunnel_type, None)
        if not h:
            err = ctypes.GetLastError()
            raise WintunError("could not create the %s adapter (error %d)"
                              % (self.name, err))
        self.handle = h
        luid = ctypes.c_ulonglong(0)
        d.WintunGetAdapterLUID(h, ctypes.byref(luid))
        self.luid = luid.value
        self.index = self._luid_to_index(self.luid)
        return self

    @staticmethod
    def _luid_to_index(luid: int) -> int:
        idx = wintypes.ULONG(0)
        iph = ctypes.windll.iphlpapi
        iph.ConvertInterfaceLuidToIndex.argtypes = [
            ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(wintypes.ULONG)]
        iph.ConvertInterfaceLuidToIndex.restype = ctypes.c_long
        if iph.ConvertInterfaceLuidToIndex(ctypes.byref(
                ctypes.c_ulonglong(luid)), ctypes.byref(idx)) != 0:
            return 0
        return int(idx.value)

    # -- configuration, all on OUR OWN adapter only ----------------------
    def set_ip(self, ip: str, prefix: int = 24) -> tuple:
        """Address this adapter, and make sure Windows will actually use it.

        A TUN adapter reports its media as Disconnected, so Duplicate
        Address Detection never completes and the address sits at
        Tentative forever. Measured: ping tolerated that, but a Tentative
        address cannot be bound and is skipped for source selection, which
        is exactly the failure that made an earlier approach look broken.

        dadtransmits=0 is set on THIS adapter only - it is ours, it has no
        neighbours, and there is nothing for DAD to discover.
        """
        _run(["netsh", "interface", "ipv4", "set", "interface",
              "%d" % self.index, "dadtransmits=0", "store=active"])
        mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
        mask_s = ".".join(str((mask >> s) & 0xFF) for s in (24, 16, 8, 0))
        return _run(["netsh", "interface", "ipv4", "set", "address",
                     "name=%d" % self.index, "source=static",
                     "addr=%s" % ip, "mask=%s" % mask_s])

    def set_mtu(self, mtu: int = 1380) -> tuple:
        return _run(["netsh", "interface", "ipv4", "set", "subinterface",
                     "%d" % self.index, "mtu=%d" % mtu, "store=active"])

    def add_route(self, cidr: str, gateway: str = "") -> tuple:
        args = ["netsh", "interface", "ipv4", "add", "route",
                "prefix=%s" % cidr, "interface=%d" % self.index,
                "store=active"]
        if gateway:
            args.insert(6, "nexthop=%s" % gateway)
        return _run(args)

    def close(self) -> None:
        if self.handle:
            _load().WintunCloseAdapter(self.handle)
            self.handle = None

    def __enter__(self):
        return self.create()

    def __exit__(self, *a):
        self.close()


class Session:
    """The packet ring. recv() blocks until a packet or the timeout.

    ORDER MATTERS, and it is not obvious: START THE SESSION BEFORE GIVING
    THE ADAPTER AN ADDRESS.

    A Wintun adapter reports its media as connected only while a session
    is open. Address it while the adapter still reads Disconnected and
    Windows marks that address Deprecated - it stays usable for an
    existing connection but is NOT chosen as the source for a new one, so
    outbound traffic from the PLC software would quietly take the wrong
    path or fail. Measured:

        session after addressing : Status=Disconnected, AddressState=Deprecated
        session before addressing: Status=Up,           AddressState=Preferred
    """

    def __init__(self, adapter: Adapter, capacity: int = DEFAULT_RING):
        if capacity < MIN_RING or capacity > MAX_RING or \
                (capacity & (capacity - 1)):
            raise WintunError("ring capacity must be a power of two "
                              "between %d and %d" % (MIN_RING, MAX_RING))
        self.adapter = adapter
        self.capacity = capacity
        self.handle = None
        self.event = None

    def start(self) -> "Session":
        d = _load()
        h = d.WintunStartSession(self.adapter.handle, self.capacity)
        if not h:
            raise WintunError("could not start the packet session (error %d)"
                              % ctypes.GetLastError())
        self.handle = h
        self.event = d.WintunGetReadWaitEvent(h)
        return self

    def recv(self, timeout_ms: int = 250):
        """One IP packet, or None on timeout."""
        d = _load()
        size = wintypes.DWORD(0)
        p = d.WintunReceivePacket(self.handle, ctypes.byref(size))
        if p:
            try:
                return bytes(bytearray(p[:size.value]))
            finally:
                d.WintunReleaseReceivePacket(self.handle, p)
        if ctypes.GetLastError() != ERROR_NO_MORE_ITEMS:
            raise WintunError("receive failed (error %d)"
                              % ctypes.GetLastError())
        rc = ctypes.windll.kernel32.WaitForSingleObject(self.event,
                                                        timeout_ms)
        if rc == WAIT_TIMEOUT:
            return None
        if rc != WAIT_OBJECT_0:
            raise WintunError("wait failed (%d)" % rc)
        return self.recv(0) if timeout_ms else None

    def send(self, packet: bytes) -> None:
        if not packet or len(packet) > MAX_IP_PACKET:
            raise WintunError("packet of %d bytes is out of range"
                              % len(packet))
        d = _load()
        buf = d.WintunAllocateSendPacket(self.handle, len(packet))
        if not buf:
            # The ring is full - the far side is not draining. Dropping is
            # correct for IP; TCP will retransmit.
            return
        ctypes.memmove(buf, packet, len(packet))
        d.WintunSendPacket(self.handle, buf)

    def end(self) -> None:
        if self.handle:
            _load().WintunEndSession(self.handle)
            self.handle = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.end()
