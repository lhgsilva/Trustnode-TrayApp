# -*- coding: utf-8 -*-
"""Share the plant network with the remote PC, by bridging it.

    RSLinx (remote PC)
        |
    TN-Link  (TAP, Layer 2)
        |   Ethernet frames, over the encrypted relay
    TN-Link  (TAP, Layer 2)  --+
                               |  Windows Network Bridge  (made ONCE, by hand)
    Ethernet 3  192.168.10.110-+
        |
    PLC 192.168.10.240

The remote PC joins the plant Ethernet segment. ARP works, so the PLC
answers it directly: no NAT, no routes, no gateway on the PLC, no special
address, and nothing about the plant network changes.

WHY THE BRIDGE IS MADE BY HAND, ONCE
------------------------------------
Windows refuses to let a program create one:

    netsh bridge install
    -> "Not supported. Please go to the Network Connections folder."

and the shell's "Bridge Connections" verb needs two connections selected
at the same time, which cannot be driven one adapter at a time. So the
TN-Link adapter is created once and LEFT IN PLACE, the operator bridges
it to the plant NIC once, and after that the app only opens the adapter
and moves frames. The bridge survives reboots.

That turned out to be the better design anyway: the app never silently
reconfigures the plant network, and the operator can see exactly what is
shared.

AN EARLIER MISTAKE, RECORDED SO IT IS NOT REPEATED
--------------------------------------------------
New-NetSwitchTeam looked like a bridge and is not: it makes a NIC team
(Microsoft Network Adapter Multiplexor), which aggregates links for the
host and does NOT forward frames between members. Ping survived on a
technicality, CIP did not, and the plant PC lost its own route to the PLC
for five minutes while its address sat on the team. Teams are never used
here.
"""
from __future__ import annotations

import subprocess
import threading
import time

import tap

CLIENT_NAME = "TN-Link"
AGENT_NAME = "TN-Link"
MAX_FRAME = 2048


def _ps(cmd, timeout=180):
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-Command", cmd], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True,
                       errors="replace", timeout=timeout)
    return r.returncode == 0, (r.stdout or "").strip()


# A virtual switch can share the same subnet as the real NIC and is
# useless to bridge. VMware's VMnet1 on 192.168.10.1 was picked over the
# real Ethernet 3 on 192.168.10.110 exactly this way.
_NOT_PLANT = ("vmware", "vmnet", "hyper-v", "vethernet", "virtualbox",
              "loopback", "tap-", "wintun", "wireguard", "nordlynx",
              "openvpn", "wiresock", "bluetooth", "wan miniport",
              "multiplexor")


def _adapters() -> list:
    """(alias, ifIndex, ip, prefix, description) for every IPv4 address."""
    ok, out = _ps("Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object "
                  "{ $a = Get-NetAdapter -InterfaceIndex "
                  "$_.InterfaceIndex -ErrorAction SilentlyContinue; "
                  "'{0}|{1}|{2}|{3}|{4}' -f $_.InterfaceAlias, "
                  "$_.InterfaceIndex, $_.IPAddress, $_.PrefixLength, "
                  "$a.InterfaceDescription }")
    rows = []
    if not ok:
        return rows
    for line in out.splitlines():
        p = line.strip().split("|")
        if len(p) == 5 and p[1].isdigit():
            rows.append((p[0], int(p[1]), p[2], p[3], p[4]))
    return rows


def adapter_for_subnet(cidr: str) -> tuple:
    """The adapter that really carries the plant network.

    Prefers, in order:
      1. an existing Network Bridge on that subnet - once bridged, the
         plant PC's address lives there,
      2. a physical adapter,
      3. anything else.
    """
    import ipaddress
    net = ipaddress.ip_network(cidr, strict=False)
    bridge = physical = other = None
    for alias, idx, ip, pfx, desc in _adapters():
        try:
            if ipaddress.ip_address(ip) not in net:
                continue
        except ValueError:
            continue
        entry = (alias, idx, "%s/%s" % (ip, pfx))
        low = (desc or "").lower()
        if "network bridge" in low or alias.lower() == "network bridge":
            bridge = bridge or entry
        elif any(h in low for h in _NOT_PLANT):
            other = other or entry
        else:
            physical = physical or entry
    return bridge or physical or other or ("", 0, "")


# --------------------------------------------------------------------------
# the one-time setup on the plant PC
# --------------------------------------------------------------------------
def bridge_state(member: str = AGENT_NAME) -> dict:
    """Is there a Network Bridge, and is TN-Link a member of it?

    Membership is the ms_implat binding - measured, not guessed. The
    bridge device itself is COMPOSITEBUS\\MS_IMPLAT_MP, and its
    DESCRIPTION is "Microsoft Network Adapter Multiplexor Driver", so
    matching the description against "Network Bridge" finds nothing even
    when one exists.
    """
    ok, out = _ps(
        "$b = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object "
        "{ $_.ComponentID -like '*MS_IMPLAT_MP*' }; "
        "'BRIDGE=' + $(if ($b) { $b.Name } else { '' }); "
        "Get-NetAdapter -ErrorAction SilentlyContinue | ForEach-Object { "
        "$n = $_.Name; $m = Get-NetAdapterBinding -Name $n -ComponentID "
        "ms_implat -ErrorAction SilentlyContinue; "
        "if ($m -and $m.Enabled) { 'MEMBER=' + $n } }")
    st = {"bridge": "", "members": [], "ready": False}
    if not ok:
        return st
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("BRIDGE="):
            st["bridge"] = line[7:].strip()
        elif line.startswith("MEMBER="):
            st["members"].append(line[7:].strip())
    st["ready"] = bool(st["bridge"]) and member in st["members"]
    return st


# Everything except plain IPv4. Another vendor's bridge or filter
# protocol bound to the adapter makes Windows refuse to bridge it with
# "An unexpected error occurred while configuring the Network Bridge".
_STRIP_BINDINGS = ("vmware_bridge", "nt_ndiswg", "nt_ndiswgc", "ms_lldp",
                   "ms_lltdio", "ms_rspndr", "ms_msclient", "ms_server",
                   "ms_pacer", "ms_tcpip6")


def survey(cidr: str, member: str = AGENT_NAME) -> dict:
    """Everything the bridge path needs, in a SINGLE PowerShell call.

    The path used to ask four separate questions - adapter list, bridge
    device, bridge membership, plant address - each paying the cost of
    starting a shell.
    """
    ok, out = _ps(
        "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction "
        "SilentlyContinue | ForEach-Object { $a = Get-NetAdapter "
        "-InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue; "
        "'A|{0}|{1}|{2}|{3}|{4}' -f $_.InterfaceAlias, $_.InterfaceIndex, "
        "$_.IPAddress, $_.PrefixLength, $a.InterfaceDescription }; "
        "$b = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object "
        "{ $_.ComponentID -like '*MS_IMPLAT_MP*' }; "
        "if ($b) { 'B|' + $b.Name }; "
        "Get-NetAdapterBinding -ComponentID ms_implat -ErrorAction "
        "SilentlyContinue | Where-Object { $_.Enabled } | ForEach-Object "
        "{ 'M|' + $_.Name }")
    import ipaddress
    info = {"adapters": [], "bridge": "", "members": [], "plant": "",
            "plant_adapter": "", "ready": False}
    if not ok:
        return info
    for line in (out or "").splitlines():
        p = line.strip().split("|")
        if p[0] == "A" and len(p) == 6 and p[2].isdigit():
            info["adapters"].append((p[1], int(p[2]), p[3], p[4], p[5]))
        elif p[0] == "B" and len(p) == 2:
            info["bridge"] = p[1]
        elif p[0] == "M" and len(p) == 2:
            info["members"].append(p[1])
    info["ready"] = bool(info["bridge"]) and member in info["members"]
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return info
    bridge = physical = other = None
    for alias, idx, ip, pfx, desc in info["adapters"]:
        try:
            if ipaddress.ip_address(ip) not in net:
                continue
        except ValueError:
            continue
        entry = (alias, "%s/%s" % (ip, pfx))
        low = (desc or "").lower()
        if alias == info["bridge"] or "network bridge" in low:
            bridge = bridge or entry
        elif any(h in low for h in _NOT_PLANT):
            other = other or entry
        else:
            physical = physical or entry
    best = bridge or physical or other
    if best:
        info["plant_adapter"], info["plant"] = best
    return info


def prepare_adapter(name: str = AGENT_NAME) -> list:
    """Strip TN-Link back to IPv4 so the bridge will accept it.

    ONE PowerShell call for all of them. Ten separate calls took fourteen
    seconds, which is most of the time the operator spent waiting.
    """
    comps = ",".join("'%s'" % c for c in _STRIP_BINDINGS)
    ok, out = _ps(
        "$done=@(); foreach ($c in @(%s)) { "
        "$b = Get-NetAdapterBinding -Name '%s' -ComponentID $c "
        "-ErrorAction SilentlyContinue; "
        "if ($b -and $b.Enabled) { Disable-NetAdapterBinding -Name '%s' "
        "-ComponentID $c -ErrorAction SilentlyContinue; $done += $c } }; "
        "$done -join ','" % (comps, name, name))
    if not ok:
        return []
    return [c for c in (out or "").strip().split(",") if c]


def plant_address(cidr: str) -> str:
    """The address currently serving that subnet, as 'ip/prefix'."""
    name, idx, ip_pfx = adapter_for_subnet(cidr)
    return ip_pfx


def restore_plant_address(ip_pfx: str, cidr: str) -> tuple:
    """Put the plant address back on whatever now carries that network.

    The bridge inherits the plant NIC's job but not always its address,
    and a bridge sitting on 169.254.x.x means the plant PC cannot reach
    its own PLC.
    """
    if not ip_pfx or "/" not in ip_pfx:
        return False, "no address recorded"
    ip, _, pfx = ip_pfx.partition("/")
    prefix = int(pfx or 24)
    ok, out = _ps("$b = Get-NetAdapter -ErrorAction SilentlyContinue | "
                  "Where-Object { $_.ComponentID -like '*MS_IMPLAT_MP*' }; "
                  "if ($b) { $b.ifIndex } else { "
                  "(Get-NetAdapter -ErrorAction SilentlyContinue | "
                  "Where-Object { $_.Status -eq 'Up' } | "
                  "Select-Object -First 1).ifIndex }")
    idx = (out or "").strip().splitlines()[0].strip() if out else ""
    if not idx.isdigit():
        return False, "no interface to restore onto"
    _ps("Get-NetIPAddress -InterfaceIndex %s -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue | Where-Object { $_.IPAddress "
        "-like '169.254.*' } | Remove-NetIPAddress -Confirm:$false "
        "-ErrorAction SilentlyContinue" % idx)
    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    mask_s = ".".join(str((mask >> sh) & 0xFF) for sh in (24, 16, 8, 0))
    r = subprocess.run(["netsh", "interface", "ipv4", "set", "address",
                        "name=%s" % idx, "source=static", "addr=%s" % ip,
                        "mask=%s" % mask_s],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, errors="replace", timeout=60)
    return r.returncode == 0, "restored %s on ifIndex %s" % (ip, idx)


def plant_is_blind(cidr: str) -> bool:
    """True when nothing on this PC holds an address on the plant subnet."""
    import ipaddress
    net = ipaddress.ip_network(cidr, strict=False)
    for alias, idx, ip, pfx, desc in _adapters():
        try:
            if ipaddress.ip_address(ip) in net:
                return False
        except ValueError:
            continue
    return True


class PlantWatchdog:
    """Keep the plant address alive while a bridge exists.

    Measured three times on the live machine: the bridge drops to APIPA
    and the plant PC silently loses its own PLC. Noticing takes minutes;
    putting it back takes a second.
    """

    def __init__(self, cidr: str, ip_pfx: str, log=print):
        self.cidr = cidr
        self.ip_pfx = ip_pfx
        self.log = log
        self.alive = False
        self._t = None
        self.repairs = 0

    def start(self) -> None:
        if not self.ip_pfx:
            return
        self.alive = True
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self) -> None:
        while self.alive:
            time.sleep(10)
            if not self.alive:
                return
            try:
                if plant_is_blind(self.cidr):
                    ok, why = restore_plant_address(self.ip_pfx, self.cidr)
                    self.repairs += 1
                    self.log("plant address had been lost - %s"
                             % (why if ok else "could NOT restore: %s" % why))
            except Exception:                               # noqa: BLE001
                pass

    def stop(self) -> None:
        self.alive = False


def ensure_adapter(name: str = AGENT_NAME) -> tuple:
    """Make sure a TN-Link adapter exists. Does NOT remove anything.

    Returns (guid, created, message).
    """
    if not tap.is_admin():
        return ("", False, "administrator rights are needed")
    for guid, nm in tap.list_instances():
        if nm == name:
            return (guid, False, "already present")
    if not tap.driver_installed():
        ok, why = tap.install_driver()
        if not ok:
            return ("", False, "could not install the driver: %s" % why)
    before = {g for g, _ in tap.list_instances()}
    ok, why = tap.create_instance()
    if not ok:
        return ("", False, why)
    guid = ""
    for _ in range(20):
        time.sleep(0.5)
        fresh = [g for g, _ in tap.list_instances() if g not in before]
        if fresh:
            guid = fresh[0]
            break
    if not guid:
        return ("", False, "the adapter was created but Windows did not "
                           "report it")
    tap.rename_connection(guid, name)
    return (guid, True, "created")


def setup_instructions(plant_adapter: str, name: str = AGENT_NAME,
                       existing_bridge: str = "") -> list:
    if existing_bridge:
        return [
            "A Network Bridge already exists on this PC, but %s is not"
            % name,
            "in it yet. Add it - this takes one right-click:",
            "",
            "  1. Press Win+R, type   ncpa.cpl   and press Enter",
            "  2. Right-click   %s" % name,
            "  3. Choose   Add to Bridge",
            "",
            "%s now shows as connected, so Windows will accept it."
            % name,
        ]
    return [
        "Windows will not let a program create a network bridge:",
        "    netsh bridge install",
        "    -> \"Not supported. Please go to the Network Connections",
        "        folder to install.\"",
        "",
        "So do it ONCE, by hand, on this PC:",
        "",
        "  1. Press Win+R, type   ncpa.cpl   and press Enter",
        "  2. Select BOTH of these (Ctrl+click):",
        "         %s" % name,
        "         %s" % plant_adapter,
        "  3. Right-click either one > Bridge Connections",
        "",
        "It survives reboots, so this is a one-time step. The plant",
        "adapter's address moves onto the bridge - that is normal.",
        "If the bridge ends up with no address, give it the one the",
        "plant adapter had.",
    ]


def open_network_connections() -> None:
    subprocess.Popen(["control.exe", "ncpa.cpl"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------------
# the running endpoint
# --------------------------------------------------------------------------
BROADCAST = b"\xff" * 6

# Multicast groups that are pure noise on a remote link.
_DROP_V4_MCAST = (
    (224, 0, 0, 251),      # mDNS
    (224, 0, 0, 252),      # LLMNR
    (239, 255, 255, 250),  # SSDP / UPnP
    (224, 0, 0, 22),       # IGMP membership reports
    (224, 0, 0, 1),        # all-hosts
    (224, 0, 0, 2),        # all-routers
)


def _is_cip_implicit(dst4) -> bool:
    """EtherNet/IP Class 1 I/O multicast, 239.192.0.0/16.

    A Logix system streams this continuously. It is between the
    controller and its own I/O; the engineer's PC never needs it, and it
    is easily enough to saturate a WAN link on its own.
    """
    return dst4[0] == 239 and dst4[1] == 192


class FrameFilter:
    """Decide what is worth sending across the relay.

    Errs towards keeping anything it cannot classify - dropping a frame
    that mattered would be a silent, maddening fault.
    """

    def __init__(self):
        self.peer_macs = set()
        self.kept = 0
        self.dropped = 0
        self.reasons = {}

    def learn(self, frame: bytes) -> None:
        """Remember the far side's MAC from what it sends us."""
        if len(frame) >= 12:
            src = frame[6:12]
            if not (src[0] & 1) and src != b"\x00" * 6:
                self.peer_macs.add(src)

    def _drop(self, why: str) -> bool:
        self.dropped += 1
        self.reasons[why] = self.reasons.get(why, 0) + 1
        return False

    def allow(self, frame: bytes) -> bool:
        if len(frame) < 14:
            return self._drop("runt")
        dst = frame[0:6]
        et = int.from_bytes(frame[12:14], "big")

        # ARP FIRST, before anything else can swallow it.
        #
        # This block used to sit below a unicast test, and an ARP REPLY is
        # unicast - addressed to whoever asked. So replies were being
        # dropped as "unicast for someone else" and the remote PC saw
        # "Destination host unreachable" from its own address: Windows had
        # asked who owns the PLC's address and nothing ever came back.
        # Nothing works without ARP, so it is never filtered.
        if et == 0x0806:
            self.kept += 1
            return True

        # Unicast is kept outright. A learning bridge floods unknown
        # unicast only until it learns the port, so filtering it saves
        # almost nothing and risks exactly the kind of silent loss above.
        if not (dst[0] & 1):
            self.kept += 1
            return True

        if et == 0x86DD:                       # IPv6
            return self._drop("IPv6")
        if dst[:3] == b"\x01\x80\xc2":        # STP / LLDP reserved
            return self._drop("spanning tree / LLDP")
        if et != 0x0800:
            return self._drop("ethertype 0x%04x" % et)

        if len(frame) < 34:
            return self._drop("short IPv4")
        d4 = frame[30:34]
        proto = frame[23]
        if proto == 17 and len(frame) >= 42:   # UDP
            ihl = (frame[14] & 0x0F) * 4
            sp, dp = (int.from_bytes(frame[14 + ihl:16 + ihl], "big"),
                      int.from_bytes(frame[16 + ihl:18 + ihl], "big"))
            if {sp, dp} & {67, 68}:            # DHCP - how we get an address
                self.kept += 1
                return True
            if {sp, dp} & {137, 138}:
                return self._drop("NetBIOS")
            if dp in (1900, 5353, 5355, 3702):
                return self._drop("discovery chatter")
        if _is_cip_implicit(d4):
            return self._drop("EtherNet/IP I/O multicast")
        if tuple(d4) in _DROP_V4_MCAST:
            return self._drop("link-local multicast")
        if d4[0] >= 224 and d4[0] <= 239:
            return self._drop("other multicast")

        self.kept += 1
        return True

    def summary(self) -> str:
        total = self.kept + self.dropped
        if not total:
            return "no frames yet"
        top = sorted(self.reasons.items(), key=lambda kv: -kv[1])[:3]
        return ("%d kept, %d dropped (%.0f%% of the segment's noise)%s"
                % (self.kept, self.dropped, 100.0 * self.dropped / total,
                   ("  - mostly " + ", ".join("%s x%d" % (k, v)
                                              for k, v in top))
                   if top else ""))


class L2Endpoint:
    """A TN-Link adapter and the frame pump behind it."""

    def __init__(self, send_frame, name: str = CLIENT_NAME,
                 keep_adapter: bool = False, filter_flood: bool = True):
        self.send_frame = send_frame
        # Only the plant end needs to filter: it is the one attached to a
        # busy segment. The engineer's PC generates its own traffic only.
        self.filter = FrameFilter() if filter_flood else None
        self.name = name
        self.keep_adapter = keep_adapter
        self.guid = ""
        self.created = False
        self.dev = None
        self.alive = False
        self._pump = None
        self.stats = {"out": 0, "in": 0}

    def up(self) -> None:
        guid, created, msg = ensure_adapter(self.name)
        if not guid:
            raise tap.TapError(msg)
        self.guid = guid
        self.created = created
        # Opening the device is what sets the media to connected. Without a
        # handle held open the adapter reads "cable unplugged" and the
        # bridge carries nothing.
        self.dev = tap.Tap(self.guid).open()
        self.alive = True
        self._pump = threading.Thread(target=self._run, daemon=True)
        self._pump.start()

    def _run(self) -> None:
        while self.alive:
            try:
                # Overlapped read: returns None on timeout instead of
                # blocking forever, which is what lets writes through.
                f = self.dev.read(MAX_FRAME, timeout_ms=500)
            except Exception:                               # noqa: BLE001
                return
            if not f:
                continue
            if self.filter is not None and not self.filter.allow(f):
                continue
            try:
                self.send_frame(f)
                self.stats["out"] += 1
            except Exception:                               # noqa: BLE001
                return

    def inject(self, frame: bytes) -> None:
        if not self.alive or not self.dev:
            return
        if self.filter is not None:
            # Whatever the far side sends tells us its MAC, so we know
            # which traffic is worth sending back to it.
            self.filter.learn(frame)
        try:
            self.dev.write(frame)
            self.stats["in"] += 1
        except Exception:                                   # noqa: BLE001
            pass

    def down(self) -> None:
        """Stop relaying. The adapter is left in place when it is part of a
        bridge the operator built - removing it would break their setup and
        make the next session need the manual step again."""
        self.alive = False
        try:
            if self.dev:
                self.dev.close()
        except Exception:
            pass
        self.dev = None
        if self.created and not self.keep_adapter:
            ok, why = tap.remove_instance(guid=self.guid)
            if not ok:
                print("  note: could not remove the TN-Link adapter - %s"
                      % why)

    # -- the client's address on the shared segment -----------------------
    def _ifindex(self) -> str:
        ok, idx = _ps("(Get-NetAdapter | Where-Object { $_.InterfaceGuid "
                      "-eq '%s' }).ifIndex" % self.guid)
        return (idx or "").strip()

    def set_ip(self, ip: str, prefix: int) -> tuple:
        idx = self._ifindex()
        if not idx:
            return False, "could not find the adapter"
        _ps("netsh interface ipv4 set interface %s dadtransmits=0 "
            "store=active" % idx)
        mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
        mask_s = ".".join(str((mask >> s) & 0xFF) for s in (24, 16, 8, 0))
        r = subprocess.run(["netsh", "interface", "ipv4", "set", "address",
                            "name=%s" % idx, "source=static",
                            "addr=%s" % ip, "mask=%s" % mask_s],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, errors="replace", timeout=60)
        return r.returncode == 0, (r.stdout or "").strip()[:160]

    def use_dhcp(self, wait_s: float = 12.0) -> tuple:
        """Ask the shared network for an address, and give up quickly.

        `ipconfig /renew` blocks for about a minute when nothing answers,
        which is why "Automatic" felt like a hang before it failed. The
        renew is fired off without waiting on it, and the address is
        polled instead - so a network with DHCP answers in a second or
        two, and one without it fails in twelve rather than sixty.
        """
        idx = self._ifindex()
        if not idx:
            return False, "could not find the adapter"
        # Clear any static address first, or DHCP has nothing to replace.
        _ps("Get-NetIPAddress -InterfaceIndex %s -AddressFamily IPv4 "
            "-ErrorAction SilentlyContinue | Where-Object "
            "{ $_.PrefixOrigin -eq 'Manual' } | Remove-NetIPAddress "
            "-Confirm:$false -ErrorAction SilentlyContinue; "
            "Set-NetIPInterface -InterfaceIndex %s -Dhcp Enabled "
            "-ErrorAction SilentlyContinue" % (idx, idx), timeout=40)
        try:
            subprocess.Popen(["ipconfig", "/renew", self.name],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception:                                   # noqa: BLE001
            pass
        deadline = time.time() + wait_s
        while time.time() < deadline:
            got = self.address()
            if got:
                return True, got
            time.sleep(1.0)
        return False, ("no DHCP answer after %.0fs - this network may not "
                       "have a DHCP server" % wait_s)

    def address(self) -> str:
        ok, out = _ps("(Get-NetIPAddress -InterfaceIndex %s -AddressFamily "
                      "IPv4 -ErrorAction SilentlyContinue | Where-Object "
                      "{ $_.IPAddress -notlike '169.254.*' }).IPAddress "
                      "-join ','" % self._ifindex(), timeout=25)
        return (out or "").strip()
