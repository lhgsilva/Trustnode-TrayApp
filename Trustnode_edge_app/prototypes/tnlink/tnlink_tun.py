# -*- coding: utf-8 -*-
"""The TN-Link tunnel: a real adapter at each end, raw IP in between.

    RSLinx -> TN-Link (client) -> encrypted link -> TN-Link (agent)
                                                 -> Windows routes + NAT
                                                 -> 192.168.10.240 (PLC)

Windows does every bit of the TCP work at BOTH ends, so this module only
moves IP packets. That is why there is no userspace TCP stack here, and
why nothing on the PLC or the plant PC has to change.

Two rules learned the hard way (see wintun.py for the measurements):

  * start the packet session BEFORE addressing the adapter, or the
    address ends up Deprecated and Windows will not use it as a source;
  * turn DAD off on our own adapter, or the address sits Tentative
    forever and cannot even be bound.

Everything this module changes is recorded and undone on teardown, and
every Windows change uses store=active so a crash cannot outlive a
reboot. Taking a machine off the internet once was enough.
"""
from __future__ import annotations

import ipaddress
import socket
import struct
import subprocess
import threading
import time

import wintun

NAT_NAME = "TrustNodeLink"
TUNNEL_CIDR = "10.77.0.0/24"
AGENT_IP = "10.77.0.1"
CLIENT_IP = "10.77.0.2"
PREFIX = 24
MTU = 1380


def _run(args, timeout=40):
    try:
        r = subprocess.run(args, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True,
                           errors="replace", timeout=timeout)
        return r.returncode == 0, (r.stdout or "").strip()
    except Exception as exc:                                # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)


def _ps(cmd, timeout=60):
    return _run(["powershell", "-NoProfile", "-NonInteractive",
                 "-Command", cmd], timeout)


def local_prefixes(ignore_adapter: str = "TN-Link") -> list:
    """Every IPv4 subnet this machine is directly on.

    Our own TN-Link adapter is skipped. A leftover one from an earlier
    session sat on the plant range and made the conflict check refuse to
    route the very network it was there to reach - the tool arguing with
    itself.
    """
    # 2026-10-29, from a live site: the customer's PLC was on 192.168.0.1
    # and TN-Link refused to share 192.168.0.0/24 saying "this PC is
    # already on" it - while ipconfig on that VM showed no 192.168.0.x
    # anywhere. The two were asking Windows different questions.
    # Get-NetIPAddress reports the address of an adapter whose cable is
    # OUT; ipconfig prints "Media disconnected" and no address at all. The
    # VM had a leftover static address on a disconnected NIC.
    #
    # Measured here the same day: this machine's disconnected "Ethernet"
    # reports 192.168.1.41/24 in state Deprecated, and two dead VPN
    # adapters report Tentative addresses. None can carry a packet.
    #
    # An adapter with no link cannot clash with anything, so only
    # CONNECTED interfaces count. ConnectionState comes from
    # Get-NetIPInterface, joined on the interface index.
    ok, out = _ps("$up = @{}; "
                  "Get-NetIPInterface -AddressFamily IPv4 | "
                  "Where-Object { $_.ConnectionState -eq 'Connected' } | "
                  "ForEach-Object { $up[$_.InterfaceIndex] = $true }; "
                  "Get-NetIPAddress -AddressFamily IPv4 | "
                  "Where-Object { $_.IPAddress -notlike '127.*' -and "
                  "$_.IPAddress -notlike '169.254.*' -and "
                  "$_.AddressState -ne 'Invalid' -and "
                  "$up.ContainsKey($_.InterfaceIndex) -and "
                  "$_.InterfaceAlias -notlike '%s*' } | "
                  "ForEach-Object { '{0}/{1}' -f $_.IPAddress, "
                  "$_.PrefixLength }" % ignore_adapter)
    out_list = []
    if not ok:
        return out_list
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out_list.append(str(ipaddress.ip_network(line, strict=False)))
        except ValueError:
            pass
    return sorted(set(out_list))


def conflicts(remote_cidrs: list) -> list:
    """Remote subnets this machine is already on - routing them would break
    the machine's own networking, so they are refused rather than guessed."""
    mine = [ipaddress.ip_network(c) for c in local_prefixes()]
    bad = []
    for c in remote_cidrs:
        try:
            net = ipaddress.ip_network(c, strict=False)
        except ValueError:
            continue
        for m in mine:
            if net.overlaps(m):
                bad.append((str(net), str(m)))
                break
    return bad


class Endpoint:
    """One end of the tunnel: the adapter, its config, and the pump."""

    def __init__(self, ip: str, send_packet, name: str = "TN-Link"):
        self.ip = ip
        self.name = name
        self.send_packet = send_packet       # callable(bytes) -> to peer
        self.adapter = None
        self.session = None
        self.alive = False
        self._pump = None
        self._undo = []                      # (description, argv)
        self.stats = {"out": 0, "in": 0, "dropped": 0}

    # -- lifecycle --------------------------------------------------------
    def up(self) -> None:
        if not wintun.is_admin():
            raise wintun.WintunError(
                "the TN-Link adapter needs administrator rights")
        self.adapter = wintun.Adapter(self.name, self.name).create()
        # Session first - see wintun.Session for why.
        self.session = wintun.Session(self.adapter).start()
        for _ in range(40):
            ok, st = _ps("(Get-NetAdapter -InterfaceIndex %d)."
                         "Status" % self.adapter.index)
            if ok and st.strip().lower() == "up":
                break
            time.sleep(0.25)
        self.adapter.set_ip(self.ip, PREFIX)
        self.adapter.set_mtu(MTU)
        for _ in range(40):
            ok, st = _ps("(Get-NetIPAddress -InterfaceIndex %d "
                         "-AddressFamily IPv4 -ErrorAction SilentlyContinue"
                         " | Where-Object { $_.IPAddress -eq '%s' })."
                         "AddressState" % (self.adapter.index, self.ip))
            if ok and st.strip().lower() == "preferred":
                break
            time.sleep(0.25)
        self.alive = True
        self._pump = threading.Thread(target=self._run_pump, daemon=True)
        self._pump.start()

    def _run_pump(self) -> None:
        while self.alive:
            try:
                pkt = self.session.recv(200)
            except Exception:                               # noqa: BLE001
                return
            if not pkt:
                continue
            try:
                self.send_packet(pkt)
                self.stats["out"] += 1
            except Exception:                               # noqa: BLE001
                self.stats["dropped"] += 1
                return

    def inject(self, pkt: bytes) -> None:
        """A packet from the peer, handed to Windows."""
        if not self.alive or not self.session:
            return
        try:
            self.session.send(pkt)
            self.stats["in"] += 1
        except Exception:                                   # noqa: BLE001
            self.stats["dropped"] += 1

    def down(self) -> None:
        self.alive = False
        for desc, argv in reversed(self._undo):
            ok, out = _run(argv)
            if not ok:
                print("  could not undo %s: %s" % (desc, out[:90]))
        self._undo = []
        try:
            if self.session:
                self.session.end()
        except Exception:
            pass
        try:
            if self.adapter:
                self.adapter.close()
        except Exception:
            pass
        self.session = self.adapter = None

    # -- the agent side: forward and NAT ---------------------------------
    def enable_forwarding(self, lan_index: int) -> tuple:
        """Let Windows route between the tunnel and the plant network.

        Both the tunnel adapter and the LAN adapter must forward. Previous
        values are recorded so this leaves the machine as it found it.
        """
        msgs = []
        for idx in (self.adapter.index, lan_index):
            ok, prev = _ps("(Get-NetIPInterface -InterfaceIndex %d "
                           "-AddressFamily IPv4).Forwarding" % idx)
            was = (prev or "").strip().lower()
            if was == "enabled":
                msgs.append("ifIdx %d already forwarding" % idx)
                continue
            ok, out = _ps("Set-NetIPInterface -InterfaceIndex %d "
                          "-AddressFamily IPv4 -Forwarding Enabled" % idx)
            if ok:
                self._undo.append(
                    ("forwarding on ifIdx %d" % idx,
                     ["powershell", "-NoProfile", "-NonInteractive",
                      "-Command",
                      "Set-NetIPInterface -InterfaceIndex %d -AddressFamily "
                      "IPv4 -Forwarding Disabled" % idx]))
                msgs.append("enabled forwarding on ifIdx %d" % idx)
            else:
                msgs.append("could not enable forwarding on ifIdx %d: %s"
                            % (idx, out[:60]))
        return True, "; ".join(msgs)

    def enable_nat(self) -> tuple:
        """SNAT tunnel traffic to the plant PC's own LAN address.

        This is what removes the need to touch the PLC: it has no default
        gateway and could never reply to 10.77.0.x, but it already talks
        to the plant PC every second.
        """
        _ps("Remove-NetNat -Name '%s' -Confirm:$false -ErrorAction "
            "SilentlyContinue" % NAT_NAME)
        ok, out = _ps("New-NetNat -Name '%s' "
                      "-InternalIPInterfaceAddressPrefix '%s' "
                      "-ErrorAction Stop | Out-Null" % (NAT_NAME, TUNNEL_CIDR))
        if not ok:
            low = (out or "").lower()
            if "already" in low or "exists" in low:
                ok = True
            else:
                return False, (out or "New-NetNat failed").splitlines()[0][:150]
        self._undo.append(
            ("the NAT rule",
             ["powershell", "-NoProfile", "-NonInteractive", "-Command",
              "Remove-NetNat -Name '%s' -Confirm:$false "
              "-ErrorAction SilentlyContinue" % NAT_NAME]))
        return True, "NAT %s -> the plant network" % TUNNEL_CIDR

    # -- the client side: routes -----------------------------------------
    def add_host_routes(self, ips: list) -> tuple:
        """Route single devices by their REAL address, over the tunnel.

        2026-10-28, measured on a machine whose WiFi is 192.168.0.161/24:

            before : 192.168.0.222 -> WiFi
            after  : 192.168.0.222 -> (tunnel adapter)
            internet still via WiFi

        A /32 is more specific than the on-link /24, so Windows prefers it
        and ONLY that address moves. This is what lets RSLinx keep the
        address written into the PLC project - the reason the operator
        cared: a port forward on 127.0.0.1 is not the PLC, and the project
        is addressed to the real one.

        It cannot help when the address is one this PC already needs - a
        PLC at 192.168.0.1 on a client whose default gateway is also
        192.168.0.1 would send the gateway down the tunnel and take the
        internet with it. The caller checks that and says so; one address
        cannot be two machines.
        """
        done, failed = [], []
        for ip in ips:
            addr = str(ip or "").strip()
            if not addr:
                continue
            ok, out = _run(["netsh", "interface", "ipv4", "add", "route",
                            "prefix=%s/32" % addr,
                            "interface=%d" % self.adapter.index,
                            "store=active"])
            if ok:
                done.append(addr)
                self._undo.append(
                    ("host route %s" % addr,
                     ["netsh", "interface", "ipv4", "delete", "route",
                      "prefix=%s/32" % addr,
                      "interface=%d" % self.adapter.index]))
            else:
                failed.append("%s (%s)" % (addr, (out or "")[:50]))
        return done, failed

    def add_routes(self, cidrs: list) -> tuple:
        done, failed = [], []
        for c in cidrs:
            ok, out = _run(["netsh", "interface", "ipv4", "add", "route",
                            "prefix=%s" % c,
                            "interface=%d" % self.adapter.index,
                            "store=active"])
            if ok:
                done.append(c)
                self._undo.append(
                    ("route %s" % c,
                     ["netsh", "interface", "ipv4", "delete", "route",
                      "prefix=%s" % c,
                      "interface=%d" % self.adapter.index]))
            else:
                failed.append("%s (%s)" % (c, (out or "")[:50]))
        return done, failed


def address_is_in_use_locally(ip: str) -> str:
    """Is this address one THIS PC already depends on? Returns why, or "".

    Measured 2026-10-28: a /32 for the client's own default gateway sends
    the gateway down the tunnel and the machine loses the internet. That is
    not a bug to work around, it is one address being asked to be two
    machines - so it is detected and explained rather than attempted.
    """
    addr = str(ip or "").strip()
    if not addr:
        return ""
    ok, out = _ps("(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | "
                  "Sort-Object RouteMetric | Select-Object -First 1).NextHop")
    if ok and (out or "").strip() == addr:
        return "it is this PC's default gateway - routing it here would "               "take the internet with it"
    ok, out = _ps("(Get-NetIPAddress -AddressFamily IPv4 | "
                  "Where-Object { $_.IPAddress -eq '%s' }).IPAddress" % addr)
    if ok and (out or "").strip() == addr:
        return "this PC already holds that address"
    return ""


def lan_interface_for(cidr: str) -> int:
    """Which adapter is actually on the PLC network."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return 0
    ok, out = _ps("Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object "
                  "{ '{0}|{1}|{2}' -f $_.IPAddress, $_.PrefixLength, "
                  "$_.InterfaceIndex }")
    if not ok:
        return 0
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        try:
            if ipaddress.ip_address(parts[0]) in net:
                return int(parts[2])
        except ValueError:
            continue
    return 0


def describe_packet(pkt: bytes) -> str:
    """Short human description, for the diagnostic counters."""
    if len(pkt) < 20 or (pkt[0] >> 4) != 4:
        return "non-IPv4"
    ihl = (pkt[0] & 0x0F) * 4
    proto = {1: "ICMP", 6: "TCP", 17: "UDP"}.get(pkt[9], str(pkt[9]))
    src = socket.inet_ntoa(pkt[12:16])
    dst = socket.inet_ntoa(pkt[16:20])
    extra = ""
    if pkt[9] in (6, 17) and len(pkt) >= ihl + 4:
        sp, dp = struct.unpack(">HH", pkt[ihl:ihl + 4])
        extra = ":%d -> :%d" % (sp, dp)
    return "%s %s -> %s%s" % (proto, src, dst, extra)
