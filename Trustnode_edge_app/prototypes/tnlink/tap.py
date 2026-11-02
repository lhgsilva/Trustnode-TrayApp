# -*- coding: utf-8 -*-
"""A TN-Link Ethernet adapter - Layer 2, so the segment can be BRIDGED.

Why not the Layer 3 adapter: an L3 adapter carries IP packets, not
Ethernet frames, so it cannot be put into a network bridge. Everything
then has to be faked with NAT or routing, and the return path breaks -
the PLC ARPs for an address nothing on the plant wire answers for. Every
NAT route was tried and measured; see the notes in tnlink_tun.py.

Bridged at Layer 2 the remote PC is genuinely ON the plant segment: ARP
works, broadcast works, the PLC replies by itself, and PLC software finds
devices the way it does on site. Nothing on the PLC or the plant PC has
to change, and no address is special.

The driver is the prebuilt signed tap-windows6 from OpenVPN Inc., shipped
unmodified under its GPLv2 licence (vendor/TAP-LICENSE.txt, with source
available from the project). The CONNECTION is named TN-Link; the device
description stays as the driver ships it, because changing it would mean
re-signing the driver.

Needs administrator rights: installing a driver and creating an adapter
both do.
"""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import threading
from ctypes import wintypes

HWID = "tap0901"
CONN_NAME = "TN-Link"

# CTL_CODE(FILE_DEVICE_UNKNOWN=0x22, function, METHOD_BUFFERED=0, FILE_ANY_ACCESS=0)
def _ctl(function: int) -> int:
    return (0x22 << 16) | (0 << 14) | (function << 2) | 0


TAP_IOCTL_GET_MAC = _ctl(1)
TAP_IOCTL_GET_VERSION = _ctl(2)
TAP_IOCTL_GET_MTU = _ctl(3)
TAP_IOCTL_SET_MEDIA_STATUS = _ctl(6)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_SYSTEM = 0x00000004
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE = ctypes.c_void_p(-1).value
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p),
                ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)]

ADAPTER_KEY = (r"SYSTEM\CurrentControlSet\Control\Class"
               r"\{4D36E972-E325-11CE-BFC1-08002BE10318}")
CONN_KEY = r"SYSTEM\CurrentControlSet\Control\Network"


class TapError(Exception):
    pass


def _here() -> str:
    return getattr(sys, "_MEIPASS", None) or os.path.dirname(
        os.path.abspath(__file__))


def _vendor(*parts) -> str:
    return os.path.join(_here(), "vendor", *parts)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run(args, timeout=180):
    try:
        r = subprocess.run(args, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True,
                           errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or "").strip()
    except Exception as exc:                                # noqa: BLE001
        return -1, "%s: %s" % (type(exc).__name__, exc)


def _ps(cmd, timeout=120):
    rc, out = _run(["powershell", "-NoProfile", "-NonInteractive",
                    "-Command", cmd], timeout)
    return rc == 0, out


# --------------------------------------------------------------------------
# driver + adapter instances
# --------------------------------------------------------------------------
def driver_installed() -> bool:
    ok, out = _ps("(Get-PnpDevice -Class Net -ErrorAction SilentlyContinue |"
                  " Where-Object { $_.HardwareID -contains 'root\\%s' })"
                  ".Count" % HWID)
    if ok and (out or "").strip().isdigit() and int(out.strip()) > 0:
        return True
    rc, out = _run(["pnputil", "/enum-drivers"])
    return "tap0901" in (out or "").lower()


def install_driver() -> tuple:
    """Add the signed driver to the Windows driver store."""
    inf = _vendor("tap", "OemVista.inf")
    if not os.path.isfile(inf):
        return False, "the driver is missing from %s" % _vendor("tap")
    rc, out = _run(["pnputil", "/add-driver", inf, "/install"])
    if rc in (0, 259, 3010):
        return True, "driver added to the store"
    return False, (out or "pnputil returned %d" % rc).splitlines()[-1][:160]


def list_instances() -> list:
    """Every TN-Link style adapter, as (interface GUID, connection name).

    Asked of Get-NetAdapter directly. An earlier version walked the
    registry for NetCfgInstanceId and returned nothing, which also made
    the cleanup skip itself and leave an adapter behind.

    The description match deliberately anchors at the start, so another
    vendor's renamed TAP adapter on the same machine is never touched.
    """
    ok, out = _ps(
        "Get-NetAdapter -ErrorAction SilentlyContinue | "
        "Where-Object { $_.InterfaceDescription -match "
        "'^TAP-Windows Adapter V9' } | "
        "ForEach-Object { '{0}|{1}' -f $_.InterfaceGuid, $_.Name }")
    found = []
    if not ok:
        return found
    for line in (out or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) == 2 and parts[0].startswith("{"):
            found.append((parts[0], parts[1]))
    return found


def create_instance() -> tuple:
    exe = _vendor("tap", "tapinstall.exe")
    if not os.path.isfile(exe):
        return False, "tapinstall.exe is missing"
    rc, out = _run([exe, "install", _vendor("tap", "OemVista.inf"), HWID])
    if rc in (0, 3010):
        return True, "adapter created"
    return False, (out or "tapinstall returned %d" % rc).splitlines()[-1][:160]


def instance_id_for(guid: str) -> str:
    """The PnP instance id of one adapter, which is what we remove by."""
    ok, out = _ps(
        "$g='%s'; Get-PnpDevice -Class Net -ErrorAction SilentlyContinue | "
        "ForEach-Object { $id = $_.InstanceId; "
        "$a = Get-NetAdapter -ErrorAction SilentlyContinue | "
        "Where-Object { $_.PnPDeviceID -eq $id -and "
        "$_.InterfaceGuid -eq $g }; if ($a) { $id } }" % guid)
    return (out or "").strip().splitlines()[0].strip() if ok and out else ""


def remove_instance(guid: str = "", instance_id: str = "") -> tuple:
    """Remove ONE adapter - ours - and nothing else.

    NEVER by hardware id. `tapinstall remove tap0901` deletes every device
    with that id, and another vendor's VPN adapter on the same machine
    shares it. That would have removed the engineer's u-link adapter.
    """
    iid = instance_id or (instance_id_for(guid) if guid else "")
    if not iid:
        return False, "could not identify the adapter to remove"
    rc, out = _run(["pnputil", "/remove-device", iid])
    if rc in (0, 3010):
        return True, "removed %s" % iid
    return False, (out or "pnputil returned %d" % rc).splitlines()[-1][:160]


def remove_instances() -> tuple:
    """Kept only so older call sites fail loudly rather than silently
    removing somebody else's adapter."""
    raise TapError("remove_instances() is unsafe - it removed every "
                   "tap0901 device, including other vendors' VPN "
                   "adapters. Use remove_instance(guid=...) instead.")


def rename_connection(guid: str, name: str = CONN_NAME) -> tuple:
    """What the operator sees in Network Connections."""
    return _ps("$a = Get-NetAdapter -ErrorAction SilentlyContinue | "
               "Where-Object { $_.InterfaceGuid -eq '%s' }; "
               "if ($a) { if ($a.Name -ne '%s') { Rename-NetAdapter "
               "-Name $a.Name -NewName '%s' -ErrorAction Stop }; 'ok' } "
               "else { 'not found' }" % (guid, name, name))


# --------------------------------------------------------------------------
# the device itself
# --------------------------------------------------------------------------
class Tap:
    """Read and write Ethernet frames on a TN-Link adapter."""

    def __init__(self, guid: str):
        self.guid = guid
        self.handle = None
        self._rd_ev = None
        self._wr_ev = None
        self._wr_lock = threading.Lock()
        self.mac = b""
        self.index = 0

    def open(self) -> "Tap":
        """Open the adapter for OVERLAPPED (asynchronous) I/O.

        FILE_FLAG_OVERLAPPED is not optional here. On a synchronous
        handle Windows serialises every operation on that handle: the
        pump thread blocks in ReadFile waiting for a frame, and a
        WriteFile from any other thread queues behind it and does not run
        until that read completes.

        The effect was that frames arriving from the remote PC reached
        the plant network only when a read happened to finish. ARP
        requests never got out, so nothing ever answered, and the remote
        PC printed "Destination host unreachable" from its own address.
        Read and write must be able to proceed at the same time.
        """
        path = r"\\.\Global\%s.tap" % self.guid
        k = ctypes.windll.kernel32
        k.CreateFileW.restype = ctypes.c_void_p
        h = k.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, 0, None,
                          OPEN_EXISTING,
                          FILE_ATTRIBUTE_SYSTEM | FILE_FLAG_OVERLAPPED,
                          None)
        if not h or h == INVALID_HANDLE:
            raise TapError("could not open %s (error %d) - is the adapter "
                           "in use by something else?"
                           % (path, ctypes.GetLastError()))
        self.handle = h
        self._ioctl_set_media(True)
        self.mac = self._ioctl(TAP_IOCTL_GET_MAC, b"", 6)
        return self

    def _ioctl(self, code, inbuf=b"", outlen=0) -> bytes:
        k = ctypes.windll.kernel32
        out = ctypes.create_string_buffer(max(outlen, 1))
        got = wintypes.DWORD(0)
        ok = k.DeviceIoControl(ctypes.c_void_p(self.handle), code,
                               inbuf, len(inbuf), out, outlen,
                               ctypes.byref(got), None)
        if not ok:
            raise TapError("ioctl 0x%x failed (error %d)"
                           % (code, ctypes.GetLastError()))
        return out.raw[:got.value]

    def _ioctl_set_media(self, connected: bool) -> None:
        """Without this the adapter stays 'cable unplugged' and Windows
        will not route or bridge through it."""
        self._ioctl(TAP_IOCTL_SET_MEDIA_STATUS,
                    (1 if connected else 0).to_bytes(4, "little"), 4)

    def _event(self):
        k = ctypes.windll.kernel32
        k.CreateEventW.restype = wintypes.HANDLE
        return k.CreateEventW(None, True, False, None)

    def read(self, size: int = 2048, timeout_ms: int = 500):
        """One Ethernet frame, or None on timeout.

        Overlapped, so a write on the same handle is never held up by a
        read that is still waiting for a frame.
        """
        k = ctypes.windll.kernel32
        if self._rd_ev is None:
            self._rd_ev = self._event()
        buf = ctypes.create_string_buffer(size)
        got = wintypes.DWORD(0)
        ov = _OVERLAPPED()
        ov.hEvent = self._rd_ev
        k.ResetEvent(ctypes.c_void_p(self._rd_ev))
        ok = k.ReadFile(ctypes.c_void_p(self.handle), buf, size,
                        ctypes.byref(got), ctypes.byref(ov))
        if not ok:
            err = ctypes.GetLastError()
            if err != ERROR_IO_PENDING:
                raise TapError("read failed (error %d)" % err)
            rc = k.WaitForSingleObject(ctypes.c_void_p(self._rd_ev),
                                       timeout_ms)
            if rc == WAIT_TIMEOUT:
                k.CancelIo(ctypes.c_void_p(self.handle))
                k.WaitForSingleObject(ctypes.c_void_p(self._rd_ev), 200)
                return None
            if rc != WAIT_OBJECT_0:
                raise TapError("read wait failed (%d)" % rc)
            if not k.GetOverlappedResult(ctypes.c_void_p(self.handle),
                                         ctypes.byref(ov),
                                         ctypes.byref(got), False):
                return None
        return buf.raw[:got.value]

    def write(self, frame: bytes) -> None:
        """Hand a frame to Windows. Never blocks behind a pending read."""
        k = ctypes.windll.kernel32
        with self._wr_lock:
            if self._wr_ev is None:
                self._wr_ev = self._event()
            wrote = wintypes.DWORD(0)
            ov = _OVERLAPPED()
            ov.hEvent = self._wr_ev
            k.ResetEvent(ctypes.c_void_p(self._wr_ev))
            ok = k.WriteFile(ctypes.c_void_p(self.handle), frame,
                             len(frame), ctypes.byref(wrote),
                             ctypes.byref(ov))
            if not ok:
                err = ctypes.GetLastError()
                if err != ERROR_IO_PENDING:
                    raise TapError("write failed (error %d)" % err)
                rc = k.WaitForSingleObject(ctypes.c_void_p(self._wr_ev),
                                           2000)
                if rc == WAIT_TIMEOUT:
                    k.CancelIo(ctypes.c_void_p(self.handle))
                    raise TapError("write timed out")
                k.GetOverlappedResult(ctypes.c_void_p(self.handle),
                                      ctypes.byref(ov),
                                      ctypes.byref(wrote), False)

    def close(self) -> None:
        if self.handle:
            try:
                self._ioctl_set_media(False)
            except Exception:
                pass
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None
        for ev in ("_rd_ev", "_wr_ev"):
            h = getattr(self, ev, None)
            if h:
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))
                setattr(self, ev, None)

    def __enter__(self):
        return self.open()

    def __exit__(self, *a):
        self.close()


def describe_frame(f: bytes) -> str:
    """Enough to tell ARP from IP in a log line."""
    if len(f) < 14:
        return "runt (%d bytes)" % len(f)
    et = int.from_bytes(f[12:14], "big")
    dst = ":".join("%02x" % b for b in f[0:6])
    src = ":".join("%02x" % b for b in f[6:12])
    if et == 0x0806 and len(f) >= 42:
        op = int.from_bytes(f[20:22], "big")
        spa = ".".join(str(b) for b in f[28:32])
        tpa = ".".join(str(b) for b in f[38:42])
        return "ARP %s %s -> %s" % ("request" if op == 1 else "reply",
                                    spa, tpa)
    if et == 0x0800 and len(f) >= 34:
        return "IPv4 %s -> %s" % (".".join(str(b) for b in f[26:30]),
                                  ".".join(str(b) for b in f[30:34]))
    return "ethertype 0x%04x %s -> %s" % (et, src, dst)
